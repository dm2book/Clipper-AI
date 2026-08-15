import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { Notice } from "../components/ui";
import type { MessageResponse } from "../api/types";

/**
 * Ask for a reset link — the page a forgotten password starts on.
 *
 * The confirmation is shown for any address at all. The API answers
 * identically whether or not the address is registered, so telling the visitor
 * "no such account" would hand back exactly the fact the endpoint refuses to
 * disclose.
 */
export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.post<MessageResponse>(
        "/auth/password/reset-request",
        { email },
      );
      setSent(result.message);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not reach the API.",
      );
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

        {sent && <Notice>{sent}</Notice>}
        {error && <Notice tone="bad">{error}</Notice>}

        {!sent && (
          <>
            <p className="small faint" style={{ marginBottom: 14 }}>
              We will email a link that sets a new password.
            </p>
            <div className="login-field">
              <label htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <button
              className="btn btn-primary"
              style={{ width: "100%" }}
              disabled={busy}
              type="submit"
            >
              {busy ? "Sending…" : "Send reset link"}
            </button>
          </>
        )}

        <p className="small faint" style={{ marginTop: 16 }}>
          <Link to="/">Back to sign in</Link>
        </p>
      </form>
    </div>
  );
}

/**
 * Spend a reset token on a new password.
 *
 * Every session for the account dies when this succeeds, including any the
 * attacker holds — which is the point of a reset and is why the page sends the
 * visitor back to sign in rather than logging them straight in.
 */
export function ResetPassword() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (password !== confirm) {
      setError("Those two passwords are not the same.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.post<MessageResponse>("/auth/password/reset", {
        token,
        new_password: password,
      });
      setDone(result.message);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not reach the API.",
      );
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

        {done && <Notice>{done}</Notice>}
        {error && (
          <div style={{ marginBottom: 14 }}>
            <Notice tone="bad">{error}</Notice>
          </div>
        )}
        {!token && !done && (
          <div style={{ marginBottom: 14 }}>
            <Notice tone="bad">
              That link is missing its token. Copy the whole URL from the email.
            </Notice>
          </div>
        )}

        {!done && (
          <>
            <div className="login-field">
              <label htmlFor="password">New password</label>
              <input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="login-field">
              <label htmlFor="confirm">Repeat it</label>
              <input
                id="confirm"
                type="password"
                autoComplete="new-password"
                required
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            <button
              className="btn btn-primary"
              style={{ width: "100%" }}
              disabled={busy || !token}
              type="submit"
            >
              {busy ? "Setting…" : "Set password"}
            </button>
          </>
        )}

        <p className="small faint" style={{ marginTop: 16 }}>
          <Link to="/">Go to sign in</Link>
        </p>
      </form>
    </div>
  );
}
