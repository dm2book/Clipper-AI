import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { Notice } from "../components/ui";
import type { MessageResponse } from "../api/types";

/**
 * Create an account.
 *
 * The success state is deliberately not "you are signed in". Signup returns a
 * message that is identical whether or not the address was already registered
 * — that sameness is the whole defence against someone testing a list of a
 * million addresses to learn which of your customers work where — so this page
 * cannot know whether it created anything, and does not pretend to.
 *
 * It therefore shows the API's own sentence rather than one of its own. A
 * cheerful "Account created!" here would be a lie in exactly the case the
 * server went to trouble to hide.
 */
export function Signup() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.post<MessageResponse>("/auth/signup", {
        email,
        password,
      });
      setSent(result.message);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not reach the API.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    setBusy(true);
    try {
      await api.post<MessageResponse>("/auth/verify/resend", { email });
    } catch {
      // Same uninformative contract; nothing useful to report either way.
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <div className="login-wrap">
        <div className="card login-card">
          <div className="brand" style={{ padding: "0 0 18px" }}>
            <span className="brand-mark">C</span>
            ClipForge AI
          </div>
          <Notice>{sent}</Notice>
          <p className="small faint" style={{ marginTop: 14 }}>
            The link confirms <strong>{email}</strong>. It expires, so if you
            leave it a day you will need a new one.
          </p>
          <button
            className="btn"
            style={{ width: "100%", marginTop: 12 }}
            disabled={busy}
            onClick={resend}
            type="button"
          >
            {busy ? "Sending…" : "Send it again"}
          </button>
          <p className="small faint" style={{ marginTop: 16 }}>
            <Link to="/">Back to sign in</Link>
          </p>
        </div>
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
          <input
            id="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>

        <div className="login-field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <span className="small faint">
            Long beats complicated. The server checks length and rejects
            anything close to your address.
          </span>
        </div>

        <button
          className="btn btn-primary"
          style={{ width: "100%" }}
          disabled={busy}
          type="submit"
        >
          {busy ? "Creating…" : "Create account"}
        </button>

        <p className="small faint" style={{ marginTop: 16 }}>
          Already have one? <Link to="/">Sign in</Link>
        </p>
      </form>
    </div>
  );
}
