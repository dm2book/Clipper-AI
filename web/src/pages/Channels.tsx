import { useState } from "react";
import { PageHead } from "../components/Shell";
import { api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import { Async, Bar, Card, Notice, Pill, money, when } from "../components/ui";
import { query } from "../api/client";
import type { ChannelOut, PageChannelOut } from "../api/types";

const STATES = ["", "active", "paused", "draft", "circuit_open"];

export function Channels() {
  const [state, setState] = useState("");
  const channels = useResource<PageChannelOut>(
    `/channels${query({ state, limit: 100 })}`,
  );
  const setChannelState = useAction(async (id: string, next: string) => {
    await api.patch(`/channels/${id}/state`, { state: next });
    channels.reload();
  });

  return (
    <>
      <PageHead
        title="Channels"
        sub="Each one owns a niche, a monthly budget and a posting cadence, and stops itself when it fails repeatedly."
      />

      <div className="toolbar">
        <label htmlFor="state">State</label>
        <select id="state" value={state} onChange={(e) => setState(e.target.value)}>
          {STATES.map((option) => (
            <option key={option} value={option}>{option || "all"}</option>
          ))}
        </select>
      </div>

      {setChannelState.error && (
        <div style={{ marginBottom: 14 }}>
          <Notice tone="bad">{setChannelState.error.message}</Notice>
        </div>
      )}

      <Async
        resource={channels}
        empty={{
          title: "No channels",
          detail: "A channel is created from a niche; it then finds content, clips it and schedules it on its own.",
          when: (d) => d.items.length === 0,
        }}
      >
        {(data) => (
          <Card bodyless>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Channel</th>
                    <th>State</th>
                    <th>Budget</th>
                    <th className="num">Items</th>
                    <th className="num">Published</th>
                    <th className="num">Failed</th>
                    <th>Created</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((channel) => (
                    <Row key={channel.id} channel={channel}
                         onSet={setChannelState.run} busy={setChannelState.busy} />
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </Async>
    </>
  );
}

function Row({
  channel,
  onSet,
  busy,
}: {
  channel: ChannelOut;
  onSet: (id: string, next: string) => Promise<boolean>;
  busy: boolean;
}) {
  const spent = channel.budget_spent_cents;
  const total = channel.budget_monthly_cents;
  const exhausted = total > 0 && spent >= total;

  return (
    <tr>
      <td>
        <div style={{ fontWeight: 550 }}>{channel.name}</div>
        <div className="small faint">{channel.niche} · {channel.timezone}</div>
      </td>
      <td>
        <Pill state={channel.state} />
        {channel.state === "circuit_open" && channel.last_error && (
          <div className="small" style={{ color: "var(--bad)", marginTop: 4, maxWidth: 240 }}>
            {channel.last_error}
          </div>
        )}
      </td>
      <td style={{ minWidth: 150 }}>
        <div className="small" style={{ marginBottom: 4 }}>
          {money(spent)} <span className="faint">of {money(total)}</span>
        </div>
        <Bar value={spent} total={total || 1} tone={exhausted ? "bad" : "ok"} />
      </td>
      <td className="num">{channel.total_items}</td>
      <td className="num">{channel.total_published}</td>
      <td className="num">
        {channel.total_failed > 0
          ? <span style={{ color: "var(--bad)" }}>{channel.total_failed}</span>
          : <span className="faint">0</span>}
      </td>
      <td className="small faint">{when(channel.created_at)}</td>
      <td className="num">
        {channel.state === "active" ? (
          <button className="btn btn-sm" disabled={busy}
                  onClick={() => void onSet(channel.id, "paused")}>
            Pause
          </button>
        ) : (
          <button className="btn btn-sm" disabled={busy}
                  onClick={() => void onSet(channel.id, "active")}>
            {channel.state === "circuit_open" ? "Reset & resume" : "Activate"}
          </button>
        )}
      </td>
    </tr>
  );
}
