/**
 * Data fetching, with the three states every page actually has.
 *
 * `useResource` returns `{ data, error, loading, reload }` and nothing else.
 * The point is that a page cannot forget one: `data` is `null` until it is
 * not, so rendering without handling the empty case does not type-check.
 *
 * No caching layer. At this size a page refetches on mount and that is
 * correct, predictable and about ten lines; a cache would mean deciding when
 * things go stale, which is a real design question and not one to answer by
 * accident.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./client";

export interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

export function useResource<T>(path: string | null, deps: unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState<boolean>(path !== null);
  const [nonce, setNonce] = useState(0);
  // Guards against a slow response for an old path overwriting a fast one for
  // a new path — the classic search-as-you-type bug.
  const latest = useRef(0);

  useEffect(() => {
    if (path === null) {
      setLoading(false);
      return;
    }
    const ticket = ++latest.current;
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    api
      .get<T>(path, controller.signal)
      .then((result) => {
        if (ticket === latest.current) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || ticket !== latest.current) return;
        setError(
          caught instanceof ApiError
            ? caught
            : new ApiError(0, "NETWORK", "Could not reach the API."),
        );
        setLoading(false);
      });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, nonce, ...deps]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

/** For buttons: tracks in-flight state and surfaces the failure. */
export function useAction<A extends unknown[]>(
  perform: (...args: A) => Promise<unknown>,
) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const run = useCallback(
    async (...args: A) => {
      setBusy(true);
      setError(null);
      try {
        await perform(...args);
        return true;
      } catch (caught) {
        setError(
          caught instanceof ApiError
            ? caught
            : new ApiError(0, "NETWORK", "Could not reach the API."),
        );
        return false;
      } finally {
        setBusy(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return { run, busy, error, clearError: () => setError(null) };
}
