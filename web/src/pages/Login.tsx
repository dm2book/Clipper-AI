import { useState, type FormEvent } from "react";
import { useAuth } from "../api/auth";
import { ApiError } from "../api/client";
import { Notice } from "../components/ui";

export function Login() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantId, setTenantId] = useState("");
  // Set when the API answers 409 TENANT_REQUIRED — the identity belongs to
  // several workspaces and the API refuses to guess which one to open.
  const [needsWorkspace, setNeedsWorkspace] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email, password, tenantId || undefined);
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
      </form>
    </div>
  );
}
