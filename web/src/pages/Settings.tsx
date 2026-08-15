import { useState, type FormEvent } from "react";
import { PageHead } from "../components/Shell";
import { api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import { Async, Card, Notice, Pill, exact, when } from "../components/ui";
import type { SettingsResponse } from "../api/types";

export function Settings() {
  const settings = useResource<SettingsResponse>("/settings");

  const revoke = useAction(async (sessionId: string) => {
    await api.post(`/settings/sessions/${sessionId}/revoke`);
    settings.reload();
  });

  return (
    <>
      <PageHead
        title="Settings"
        sub="Your account, where you are signed in, connected platforms, and what this deployment can actually do."
      />

      <Async resource={settings} skeletonRows={8}>
        {(data) => (
          <div className="grid" style={{ gap: 18 }}>
            <div className="grid grid-2">
              <Card title="Account">
                <dl style={{ margin: 0, display: "grid",
                             gridTemplateColumns: "auto 1fr", gap: "9px 16px" }}>
                  <dt className="dim small">Email</dt>
                  <dd style={{ margin: 0 }}>
                    {data.email}{" "}
                    {data.verified
                      ? <Pill state="active" label="verified" />
                      : <Pill state="unverified" label="unverified" />}
                  </dd>
                  <dt className="dim small">Role</dt>
                  <dd style={{ margin: 0 }}>{data.role}</dd>
                  <dt className="dim small">Workspace</dt>
                  <dd style={{ margin: 0 }} className="mono">{data.tenant_id}</dd>
                  <dt className="dim small">Identity</dt>
                  <dd style={{ margin: 0 }} className="mono">{data.identity_id}</dd>
                </dl>
              </Card>

              <ChangePassword />
            </div>

            <Card title="Workspaces" bodyless>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Workspace</th><th>Role</th><th>ID</th><th /></tr>
                  </thead>
                  <tbody>
                    {data.memberships.map((m) => (
                      <tr key={m.user_id}>
                        <td>{m.tenant_name || m.tenant_id}</td>
                        <td><span className="pill">{m.role}</span></td>
                        <td className="mono small">{m.tenant_id}</td>
                        <td className="num">
                          {m.tenant_id === data.tenant_id && (
                            <span className="pill pill-info">current</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card title="Signed in on" bodyless>
              {revoke.error && (
                <div style={{ padding: 14 }}>
                  <Notice tone="bad">{revoke.error.message}</Notice>
                </div>
              )}
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Session</th><th>Address</th><th>Started</th>
                      <th className="num">Refreshes</th><th />
                    </tr>
                  </thead>
                  <tbody>
                    {data.sessions.map((session) => (
                      <tr key={session.session_id}>
                        <td>
                          <span className="mono small">{session.session_id}</span>
                          {session.current && (
                            <span className="pill pill-info" style={{ marginLeft: 8 }}>
                              this device
                            </span>
                          )}
                          <div className="small faint truncate">
                            {session.user_agent || "unknown client"}
                          </div>
                        </td>
                        <td className="mono small">{session.ip || "—"}</td>
                        <td className="small faint" title={exact(session.issued_at)}>
                          {when(session.issued_at)}
                        </td>
                        <td className="num small">{session.rotations}</td>
                        <td className="num">
                          {!session.current && (
                            <button className="btn btn-sm btn-danger" disabled={revoke.busy}
                                    onClick={() => void revoke.run(session.session_id)}>
                              Revoke
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card title="Connected platforms" bodyless>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Platform</th><th>Handle</th><th>Status</th></tr>
                  </thead>
                  <tbody>
                    {data.accounts.length === 0 ? (
                      <tr>
                        <td colSpan={3} className="faint small"
                            style={{ textAlign: "center", padding: 22 }}>
                          No platform accounts connected.
                        </td>
                      </tr>
                    ) : (
                      data.accounts.map((account) => (
                        <tr key={account.id}>
                          <td>{account.platform}</td>
                          <td className="small">{account.handle || "—"}</td>
                          <td>
                            {account.connected
                              ? <Pill state="active" label="connected" />
                              : <Pill state="needs_attention" label="reconnect" />}
                            {account.detail && (
                              <div className="small faint" style={{ marginTop: 3 }}>
                                {account.detail}
                              </div>
                            )}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </Card>

            {/* The most useful card here, and the one most dashboards omit:
                what this build genuinely cannot do. Most answers are negative,
                and each explains a way the product will look broken. */}
            <Card title="What this deployment can do" bodyless>
              <div className="table-wrap">
                <table>
                  <tbody>
                    {data.capabilities.map((capability) => (
                      <tr key={capability.key}>
                        <td style={{ width: 190, fontWeight: 550 }}>
                          {capability.label}
                        </td>
                        <td style={{ width: 110 }}>
                          {capability.available
                            ? <Pill state="active" label="available" />
                            : <Pill state="failed" label="unavailable" />}
                        </td>
                        <td className="small dim">{capability.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
        )}
      </Async>
    </>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [done, setDone] = useState(false);

  const change = useAction(async (a: string, b: string) => {
    await api.post("/auth/password", { current_password: a, new_password: b });
    setCurrent("");
    setNext("");
    setDone(true);
  });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setDone(false);
    await change.run(current, next);
  }

  return (
    <Card title="Change password">
      <form onSubmit={submit}>
        {change.error && (
          <div style={{ marginBottom: 12 }}>
            <Notice tone="bad">{change.error.message}</Notice>
          </div>
        )}
        {done && (
          <div style={{ marginBottom: 12 }}>
            <Notice>Password changed. Every other device has been signed out.</Notice>
          </div>
        )}
        <div className="login-field">
          <label htmlFor="current">Current password</label>
          <input id="current" type="password" autoComplete="current-password"
                 required value={current} onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="login-field">
          <label htmlFor="next">New password</label>
          <input id="next" type="password" autoComplete="new-password"
                 required value={next} onChange={(e) => setNext(e.target.value)} />
          <span className="small faint">
            At least 12 characters. A long ordinary phrase beats a short complicated one.
          </span>
        </div>
        <button className="btn btn-primary" type="submit" disabled={change.busy}>
          {change.busy ? "Changing…" : "Change password"}
        </button>
      </form>
    </Card>
  );
}
