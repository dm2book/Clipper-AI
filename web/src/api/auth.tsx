/**
 * Who is signed in, for the whole app.
 *
 * On mount it asks `/auth/me` rather than trusting what is in localStorage:
 * a stored token may be expired, revoked, or from a previous deployment with
 * a different signing key, and the only authority on that is the API.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ApiError, api, onSignedOut, tokens } from "./client";
import type { LoginResponse, MeResponse } from "./types";

interface AuthState {
  me: MeResponse | null;
  loading: boolean;
  signIn: (email: string, password: string, tenantId?: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshMe: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const refreshMe = useCallback(async () => {
    if (!tokens.access()) {
      setMe(null);
      setLoading(false);
      return;
    }
    try {
      setMe(await api.get<MeResponse>("/auth/me"));
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshMe();
    // The client signs out when a refresh fails; the app has to follow.
    return onSignedOut(() => setMe(null));
  }, [refreshMe]);

  const signIn = useCallback(
    async (email: string, password: string, tenantId?: string) => {
      const result = await api.post<LoginResponse>("/auth/login", {
        email,
        password,
        tenant_id: tenantId ?? "",
      });
      tokens.set(result.tokens);
      setMe(await api.get<MeResponse>("/auth/me"));
    },
    [],
  );

  const signOut = useCallback(async () => {
    const refresh = tokens.refresh();
    try {
      if (refresh) await api.post("/auth/logout", { refresh_token: refresh });
    } catch (caught) {
      // A failed sign-out must still sign out locally. Leaving the token in
      // place because the network blipped is the opposite of what was asked.
      if (!(caught instanceof ApiError)) throw caught;
    } finally {
      tokens.clear();
      setMe(null);
    }
  }, []);

  const value = useMemo(
    () => ({ me, loading, signIn, signOut, refreshMe }),
    [me, loading, signIn, signOut, refreshMe],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const held = useContext(AuthContext);
  if (!held) throw new Error("useAuth must be used inside an AuthProvider");
  return held;
}
