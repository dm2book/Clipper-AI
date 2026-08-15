import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { Notice, Skeleton } from "../components/ui";
import type { MessageResponse } from "../api/types";

/**
 * The landing page for a link out of a verification email.
 *
 * Reachable signed in or signed out. The token is the credential, and a
 * deployment that blocks unverified sign-in would otherwise have accounts that
 * can never verify — the link would bounce to a login they are not allowed to
 * complete.
 *
 * The effect guards against running twice. React's StrictMode mounts an effect,
 * unmounts and mounts it again in development, and a verification token is
 * spent on first use — so without the guard the second call always reports an
 * invalid token and the page shows a failure for a verification that worked.
 */
export function Verify() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [state, setState] = useState<"working" | "done" | "failed">("working");
  const [message, setMessage] = useState("");
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    if (!token) {
      setState("failed");
      setMessage("That link is missing its token. Copy the whole URL from the email.");
      return;
    }
    api
      .post<MessageResponse>("/auth/verify", { token })
      .then((result) => {
        setState("done");
        setMessage(result.message);
      })
      .catch((caught) => {
        setState("failed");
        setMessage(
          caught instanceof ApiError
            ? caught.message
            : "Could not reach the API.",
        );
      });
  }, [token]);

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <div className="brand" style={{ padding: "0 0 18px" }}>
          <span className="brand-mark">C</span>
          ClipForge AI
        </div>

        {state === "working" && <Skeleton rows={2} />}
        {state === "done" && <Notice>{message}</Notice>}
        {state === "failed" && <Notice tone="bad">{message}</Notice>}

        {state === "failed" && (
          <p className="small faint" style={{ marginTop: 14 }}>
            Links expire, and each one can only be used once. Sign in and ask
            for a new one if this has already been spent.
          </p>
        )}

        <p className="small faint" style={{ marginTop: 16 }}>
          <Link to="/">Go to sign in</Link>
        </p>
      </div>
    </div>
  );
}
