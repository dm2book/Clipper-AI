import { useState } from "react";
import { PageHead } from "../components/Shell";
import { query } from "../api/client";
import { useResource } from "../api/hooks";
import { Async, Card, Notice, num, when } from "../components/ui";
import type { AnalyticsResponse, MetricSeriesOut } from "../api/types";

const WINDOWS = [7, 30, 90];

export function Analytics() {
  const [windowDays, setWindowDays] = useState(30);
  const analytics = useResource<AnalyticsResponse>(
    `/analytics${query({ window_days: windowDays })}`,
    [windowDays],
  );

  return (
    <>
      <PageHead
        title="Analytics"
        sub="Everything below is scoped to the window, and reports only what has actually been measured."
      />

      <div className="toolbar">
        <label htmlFor="window">Window</label>
        <select id="window" value={windowDays}
                onChange={(e) => setWindowDays(Number(e.target.value))}>
          {WINDOWS.map((days) => (
            <option key={days} value={days}>last {days} days</option>
          ))}
        </select>
      </div>

      <Async resource={analytics} skeletonRows={7}>
        {(data) => (
          <div className="grid" style={{ gap: 18 }}>
            {data.note && <Notice tone="warn">{data.note}</Notice>}

            <div className="grid grid-stats">
              <Stat label="Posts measured" value={num(data.posts_measured)} />
              <Stat label="Views" value={num(data.total_views)} />
              <Stat label="Likes" value={num(data.total_likes)} />
              <Stat
                label="Average watched"
                value={
                  data.avg_watch_pct === null || data.avg_watch_pct === undefined
                    ? "—"
                    : `${num(data.avg_watch_pct, 1)}%`
                }
              />
            </div>

            {data.series.length > 0 && (
              <div className="grid grid-2">
                {data.series.map((series) => (
                  <Card key={series.key} title={series.label}>
                    <Chart series={series} />
                  </Card>
                ))}
              </div>
            )}

            <div className="grid grid-2">
              <Card title="By platform" bodyless>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Platform</th>
                        <th className="num">Posts</th>
                        <th className="num">Views</th>
                        <th className="num">Likes</th>
                        <th className="num">Watched</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_platform.length === 0 ? (
                        <tr>
                          <td colSpan={5} className="faint small"
                              style={{ textAlign: "center", padding: 22 }}>
                            Nothing published in this window.
                          </td>
                        </tr>
                      ) : (
                        data.by_platform.map((row) => (
                          <tr key={row.platform}>
                            <td>{row.platform}</td>
                            <td className="num">{row.posts}</td>
                            <td className="num">{num(row.views)}</td>
                            <td className="num">{num(row.likes)}</td>
                            <td className="num">
                              {row.avg_watch_pct === null || row.avg_watch_pct === undefined
                                ? "—"
                                : `${num(row.avg_watch_pct, 1)}%`}
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </Card>

              <Card title="Top posts" bodyless>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Title</th>
                        <th className="num">Views</th>
                        <th>Published</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.top.length === 0 ? (
                        <tr>
                          <td colSpan={3} className="faint small"
                              style={{ textAlign: "center", padding: 22 }}>
                            Nothing measured in this window.
                          </td>
                        </tr>
                      ) : (
                        data.top.map((video) => (
                          <tr key={video.upload_id}>
                            <td className="truncate">{video.title || video.upload_id}</td>
                            <td className="num">{num(video.views)}</td>
                            <td className="small faint">{when(video.published_at)}</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </Card>
            </div>
          </div>
        )}
      </Async>
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

/**
 * A plain SVG line chart. No charting library: this draws one series with a
 * baseline and two axis labels, which is about forty lines, and a dependency
 * would bring a rendering model, a theme system and a bundle for it.
 */
function Chart({ series }: { series: MetricSeriesOut }) {
  const points = series.points;
  if (points.length === 0) {
    return <p className="faint small" style={{ margin: 0 }}>No data in this window.</p>;
  }

  const width = 520;
  const height = 170;
  const pad = { top: 12, right: 10, bottom: 22, left: 44 };
  const max = Math.max(...points.map((p) => p.value), 1);
  const inner = { w: width - pad.left - pad.right, h: height - pad.top - pad.bottom };

  const x = (index: number) =>
    pad.left + (points.length === 1 ? inner.w / 2 : (index / (points.length - 1)) * inner.w);
  const y = (value: number) => pad.top + inner.h - (value / max) * inner.h;

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(p.value)}`).join(" ");
  const area = `${line} L${x(points.length - 1)},${pad.top + inner.h} L${x(0)},${pad.top + inner.h} Z`;

  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`}
         preserveAspectRatio="none" role="img"
         aria-label={`${series.label}: ${points.length} points, peak ${max}`}>
      <line className="chart-grid" x1={pad.left} y1={pad.top + inner.h}
            x2={width - pad.right} y2={pad.top + inner.h} />
      <line className="chart-grid" x1={pad.left} y1={pad.top}
            x2={width - pad.right} y2={pad.top} strokeDasharray="3 3" />
      <path className="chart-area" d={area} />
      <path className="chart-line" d={line} />
      <text className="chart-label" x={4} y={pad.top + 4}>{num(max)}</text>
      <text className="chart-label" x={4} y={pad.top + inner.h + 4}>0</text>
      <text className="chart-label" x={pad.left} y={height - 6}>
        {new Date(points[0]!.at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
      </text>
      <text className="chart-label" x={width - pad.right} y={height - 6} textAnchor="end">
        {new Date(points[points.length - 1]!.at).toLocaleDateString(undefined,
          { month: "short", day: "numeric" })}
      </text>
    </svg>
  );
}
