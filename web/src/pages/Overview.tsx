import { PageHead } from "../components/Shell";
import { useResource } from "../api/hooks";
import { Async, Bar, Card, Empty, Notice, Pill, num, when } from "../components/ui";
import type { OverviewResponse } from "../api/types";

export function Overview() {
  const overview = useResource<OverviewResponse>("/overview");

  return (
    <>
      <PageHead
        title="Overview"
        sub="Everything counted from this workspace's own rows, at the moment you asked."
        action={
          <button className="btn btn-sm" onClick={overview.reload}
                  disabled={overview.loading}>
            {overview.loading ? "Refreshing…" : "Refresh"}
          </button>
        }
      />

      <Async resource={overview} skeletonRows={8}>
        {(data) => (
          <div className="grid" style={{ gap: 18 }}>
            {data.attention.length > 0 && (
              <Notice tone="warn">
                <div>
                  <strong>Needs a look</strong>
                  <ul className="list-reset" style={{ marginTop: 6 }}>
                    {data.attention.map((note) => (
                      <li key={note} style={{ marginTop: 2 }}>· {note}</li>
                    ))}
                  </ul>
                </div>
              </Notice>
            )}

            <div className="grid grid-stats">
              {data.stats.map((stat) => (
                <div key={stat.key} className="card stat">
                  <div className="stat-label">{stat.label}</div>
                  <div className="stat-value">
                    {num(stat.value, Number.isInteger(stat.value) ? 0 : 1)}
                    {stat.unit && <span className="stat-unit">{stat.unit}</span>}
                  </div>
                  {stat.detail && <div className="stat-detail">{stat.detail}</div>}
                </div>
              ))}
            </div>

            <div className="grid grid-2">
              <Card title="Pipeline">
                {data.pipeline.map((stage) => (
                  <div key={stage.stage} className="pipeline-row">
                    <span className="pipeline-name">{stage.label}</span>
                    <span className="pipeline-bar">
                      <Bar value={stage.done} total={stage.total || 1}
                           tone={stage.failed > 0 ? "warn" : "ok"} />
                    </span>
                    <span className="pipeline-count">
                      {stage.done}/{stage.total}
                      {stage.failed > 0 && (
                        <span style={{ color: "var(--bad)" }}> ·{stage.failed}</span>
                      )}
                    </span>
                  </div>
                ))}
                <p className="small faint" style={{ marginTop: 10, marginBottom: 0 }}>
                  A stage well below the one before it, with nothing in flight, is
                  where work has stopped.
                </p>
              </Card>

              <Card title="Recent activity" bodyless>
                {data.activity.length === 0 ? (
                  <Empty title="Nothing yet"
                         detail="Activity appears as sources are acquired and posts go out." />
                ) : (
                  <div className="table-wrap">
                    <table>
                      <tbody>
                        {data.activity.slice(0, 8).map((item) => (
                          <tr key={`${item.kind}-${item.reference}-${item.at}`}>
                            <td style={{ width: 74 }}>
                              <span className="pill">{item.kind}</span>
                            </td>
                            <td className="truncate">{item.summary}</td>
                            <td style={{ width: 128 }}><Pill state={item.state} /></td>
                            <td className="num faint small" style={{ width: 74 }}>
                              {when(item.at)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>

            <p className="small faint" style={{ margin: 0 }}>
              Generated {when(data.generated_at)} · workspace{" "}
              <span className="mono">{data.tenant_id}</span>
            </p>
          </div>
        )}
      </Async>
    </>
  );
}
