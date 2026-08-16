import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../api/auth";
import { ApiError } from "../api/client";
import { Notice } from "../components/ui";
import type { MfaChallengeOut } from "../api/types";

export function Login() {
  const { signIn, completeMfa } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantId, setTenantId] = useState("");
  // Set when the API answers 409 TENANT_REQUIRED — the identity belongs to
  // several workspaces and the API refuses to guess which one to open.
  const [needsWorkspace, setNeedsWorkspace] = useState(false);
  // Set when the password was accepted and a second factor is owed. No
  // credential has been stored at this point — the challenge is all there is.
  const [challenge, setChallenge] = useState<MfaChallengeOut | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const owed = await signIn(email, password, tenantId || undefined);
      if (owed) setChallenge(owed);
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.code === "TENANT_REQUIRED") setNeedsWorkspace(true);
        setError(caught.message);
      } else {
        setError("Could not reach the API.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(event: FormEvent) {
    event.preventDefault();
    if (!challenge) return;
    setBusy(true);
    setError(null);
    try {
      await completeMfa(challenge.challenge_token, code, tenantId || undefined);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not reach the API.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (challenge) {
    return (
      <div className="login-wrap">
        <form className="card login-card" onSubmit={submitCode}>
          <div className="brand" style={{ padding: "0 0 18px" }}>
            <span className="brand-mark">C</span>
            ClipForge AI
          </div>

          {error && (
            <div style={{ marginBottom: 14 }}>
              <Notice tone="bad">{error}</Notice>
            </div>
          )}

          <div className="login-field">
            <label htmlFor="code">Authentication code</label>
            <input
              id="code"
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
              required
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            <span className="small faint">
              The six-digit code from your authenticator app
              {challenge.recovery_available
                ? ", or one of your recovery codes."
                : "."}
            </span>
          </div>

          <button className="btn btn-primary" style={{ width: "100%" }}
                  disabled={busy} type="submit">
            {busy ? "Checking…" : "Continue"}
          </button>

          <p className="small faint" style={{ marginTop: 16 }}>
            <button type="button" className="btn"
                    style={{ width: "100%" }}
                    onClick={() => { setChallenge(null); setCode(""); }}>
              Start again
            </button>
          </p>
        </form>
      </div>
    );
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand" style={{ padding: "0 0 18px" }}>
          <span className="brand-mark">C</span>
          ClipForge AI
        </div>

        {error && (
          <div style={{ marginBottom: 14 }}>
            <Notice tone="bad">{error}</Notice>
          </div>
        )}

        <div className="login-field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" autoComplete="username" required
                 value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>

        <div className="login-field">
          <label htmlFor="password">Password</label>
          <input id="password" type="password" autoComplete="current-password"
                 required value={password}
                 onChange={(e) => setPassword(e.target.value)} />
        </div>

        {needsWorkspace && (
          <div className="login-field">
            <label htmlFor="tenant">Workspace ID</label>
            <input id="tenant" value={tenantId}
                   onChange={(e) => setTenantId(e.target.value)} />
            <span className="small faint">
              This account belongs to more than one workspace.
            </span>
          </div>
        )}

        <button className="btn btn-primary" style={{ width: "100%" }}
                disabled={busy} type="submit">
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="small faint" style={{ marginTop: 16 }}>
          <Link to="/signup">Create an account</Link>
          {" · "}
          <Link to="/forgot">Forgot your password?</Link>
        </p>
      </form>
    </div>
  );
}
