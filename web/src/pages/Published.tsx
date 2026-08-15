import { useState } from "react";
import { PageHead } from "../components/Shell";
import { query } from "../api/client";
import { useResource } from "../api/hooks";
import { Async, Card, Notice, num, when } from "../components/ui";
import type { PagePublishedVideoOut } from "../api/types";

const PLATFORMS = ["", "tiktok", "youtube", "instagram"];

export function Published() {
  const [platform, setPlatform] = useState("");
  const published = useResource<PagePublishedVideoOut>(
    `/published${query({ platform, limit: 100 })}`,
  );

  const unmeasured = published.data
    ? published.data.items.filter((v) => v.views === null || v.views === undefined).length
    : 0;

  return (
    <>
      <PageHead
        title="Published Videos"
        sub="Posts that reached a platform, newest first, with the most recent measurement of each."
      />

      <div className="toolbar">
        <label htmlFor="platform">Platform</label>
        <select id="platform" value={platform}
                onChange={(e) => setPlatform(e.target.value)}>
          {PLATFORMS.map((option) => (
            <option key={option} value={option}>{option || "all"}</option>
          ))}
        </select>
        <span className="spacer" />
        {published.data && (
          <span className="small faint">{published.data.total} posts</span>
        )}
      </div>

      {unmeasured > 0 && (
        <div style={{ marginBottom: 14 }}>
          <Notice>
            <span>
              <strong>{unmeasured}</strong> of these have no measurement yet. That
              is shown as <span className="mono">—</span>, not as zero: nothing has
              collected counters for them, which is a different thing from a post
              nobody watched.
            </span>
          </Notice>
        </div>
      )}

      <Async
        resource={published}
        empty={{
          title: "Nothing published yet",
          detail: "Posts appear here once a platform confirms them.",
          when: (d) => d.items.length === 0,
        }}
      >
        {(data) => (
          <Card bodyless>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Channel</th>
                    <th>Platform</th>
                    <th className="num">Views</th>
                    <th className="num">Likes</th>
                    <th className="num">Watched</th>
                    <th>Published</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((video) => (
                    <tr key={video.upload_id}>
                      <td className="truncate" style={{ fontWeight: 550 }}>
                        {video.title || <span className="faint">untitled</span>}
                      </td>
                      <td className="small">{video.channel_name || video.channel_id}</td>
                      <td className="small">{video.platform}</td>
                      <td className="num">{num(video.views)}</td>
                      <td className="num">{num(video.likes)}</td>
                      <td className="num">
                        {video.avg_watch_pct === null || video.avg_watch_pct === undefined
                          ? "—"
                          : `${num(video.avg_watch_pct, 1)}%`}
                      </td>
                      <td className="small faint">{when(video.published_at)}</td>
                      <td className="num">
                        {video.permalink && (
                          <a className="small" href={video.permalink}
                             target="_blank" rel="noreferrer noopener">
                            open ↗
                          </a>
                        )}
                      </td>
                    </tr>
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
