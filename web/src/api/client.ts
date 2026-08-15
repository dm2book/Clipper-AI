/**
 * The one place that talks to the API.
 *
 * ## Refresh happens here, once
 *
 * An access token lives fifteen minutes, so any session longer than that will
 * hit a 401 mid-use. Handling that in each page means fifteen slightly
 * different retry loops; handling it here means pages never see it.
 *
 * Concurrent 401s share a single refresh. Six components mounting at once
 * would otherwise fire six refreshes, and because the API rotates refresh
 * tokens and treats a replayed one as theft, five of them would present a
 * token the first had already spent — revoking the whole session family and
 * logging the user out. The in-flight promise is what stops a page load from
 * looking like an attack.
 */
import type { ErrorResponse, TokenPairOut } from "./types";

const BASE = "/api/v1";
const ACCESS_KEY = "clipforge.access";
const REFRESH_KEY = "clipforge.refresh";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryAfterS?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** True when signing in again is the fix. */
  get isAuth(): boolean {
    return this.status === 401 || this.code === "NOT_AUTHENTICATED";
  }
}

/**
 * Tokens live in localStorage, which is a real trade and not an oversight.
 * A `httpOnly` cookie is not readable by injected script and would be better,
 * but it needs the API and the app on one origin plus CSRF protection on every
 * mutation. Until that is set up this is the honest option, and the access
 * token's short life is what bounds the damage.
 */
export const tokens = {
  access: (): string | null => localStorage.getItem(ACCESS_KEY),
  refresh: (): string | null => localStorage.getItem(REFRESH_KEY),
  set(pair: Pick<TokenPairOut, "access_token" | "refresh_token">) {
    localStorage.setItem(ACCESS_KEY, pair.access_token);
    localStorage.setItem(REFRESH_KEY, pair.refresh_token);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

let refreshing: Promise<string | null> | null = null;
const listeners = new Set<() => void>();

/** Called when the session ends for good, so the app can show the login. */
export function onSignedOut(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function signedOut() {
  tokens.clear();
  listeners.forEach((l) => l());
}

async function parseError(response: Response): Promise<ApiError> {
  let code = "ERROR";
  let message = response.statusText || "Request failed";
  let retryAfterS: number | undefined;
  try {
    const body = (await response.json()) as ErrorResponse;
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      retryAfterS = body.error.retry_after_s ?? undefined;
    }
  } catch {
    // A non-JSON body means something in front of the API answered — a proxy,
    // a maintenance page. The status is all there is to go on.
  }
  return new ApiError(response.status, code, message, retryAfterS);
}

async function renew(): Promise<string | null> {
  const token = tokens.refresh();
  if (!token) return null;

  const response = await fetch(`${BASE}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: token }),
  });
  if (!response.ok) {
    signedOut();
    return null;
  }
  const pair = (await response.json()) as TokenPairOut;
  tokens.set(pair);
  return pair.access_token;
}

function renewOnce(): Promise<string | null> {
  // Shared, not per-caller. See the note at the top of this file.
  refreshing ??= renew().finally(() => {
    refreshing = null;
  });
  return refreshing;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Set internally to stop a refreshed request refreshing again. */
  retried?: boolean;
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, signal, retried = false } = options;
  const access = tokens.access();

  const response = await fetch(`${BASE}${path}`, {
    method,
    signal,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  if (response.status === 401 && !retried && tokens.refresh()) {
    const renewed = await renewOnce();
    if (renewed) return request<T>(path, { ...options, retried: true });
    throw new ApiError(401, "NOT_AUTHENTICATED", "Your session has ended.");
  }

  if (!response.ok) {
    const error = await parseError(response);
    if (error.isAuth) signedOut();
    throw error;
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body }),
};

/** Build a query string, dropping empty values so the URL stays readable. */
export function query(params: Record<string, string | number | boolean | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "" || value === null) continue;
    search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}
