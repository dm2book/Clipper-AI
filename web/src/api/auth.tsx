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
import type { LoginResponse, MeResponse, MfaChallengeOut } from "./types";

interface AuthState {
  me: MeResponse | null;
  loading: boolean;
  /**
   * Resolves to a challenge when a second factor is owed, or null when the
   * session is open. Deliberately not `void`: the generated types made
   * `tokens` nullable the moment MFA landed, and `tsc` refused to compile the
   * old signature — which is the whole reason the types are generated.
   */
  signIn: (
    email: string, password: string, tenantId?: string,
  ) => Promise<MfaChallengeOut | null>;
  completeMfa: (
    challengeToken: string, code: string, tenantId?: string,
  ) => Promise<void>;
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

  const adopt = useCallback(async (result: LoginResponse) => {
    if (!result.tokens) {
      throw new Error("The server returned no tokens and no challenge.");
    }
    tokens.set(result.tokens);
    setMe(await api.get<MeResponse>("/auth/me"));
  }, []);

  const signIn = useCallback(
    async (email: string, password: string, tenantId?: string) => {
      const result = await api.post<LoginResponse>("/auth/login", {
        email,
        password,
        tenant_id: tenantId ?? "",
      });
      // A challenge is a successful password step, not a failure. The caller
      // shows a code prompt; nothing is stored, because half a login must
      // leave no credential behind.
      if (result.mfa) return result.mfa;
      await adopt(result);
      return null;
    },
    [adopt],
  );

  const completeMfa = useCallback(
    async (challengeToken: string, code: string, tenantId?: string) => {
      const result = await api.post<LoginResponse>("/auth/mfa/verify", {
        challenge_token: challengeToken,
        code,
        tenant_id: tenantId ?? "",
      });
      await adopt(result);
    },
    [adopt],
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
    () => ({ me, loading, signIn, completeMfa, signOut, refreshMe }),
    [me, loading, signIn, completeMfa, signOut, refreshMe],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const held = useContext(AuthContext);
  if (!held) throw new Error("useAuth must be used inside an AuthProvider");
  return held;
}
