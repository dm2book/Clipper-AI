import { PageHead } from "../components/Shell";
import { api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import { Async, Card, Notice, Pill, when, exact } from "../components/ui";
import type { PageUploadOut, UploadOut } from "../api/types";

export function Queue() {
  const queue = useResource<PageUploadOut>("/uploads?limit=100");
  const retry = useAction(async (id: string) => {
    await api.post(`/uploads/${id}/retry`);
    queue.reload();
  });

  return (
    <>
      <PageHead
        title="Upload Queue"
        sub="Everything not yet live, soonest first — scheduled, retrying, and the ones that have stopped and need a person."
        action={
          <button className="btn btn-sm" onClick={queue.reload} disabled={queue.loading}>
            {queue.loading ? "Refreshing…" : "Refresh"}
          </button>
        }
      />

      {retry.error && (
        <div style={{ marginBottom: 14 }}>
          <Notice tone="bad">{retry.error.message}</Notice>
        </div>
      )}

      <Async
        resource={queue}
        empty={{
          title: "The queue is empty",
          detail: "Nothing is waiting to publish. Scheduled posts appear here until they go out.",
          when: (d) => d.items.length === 0,
        }}
      >
        {(data) => (
          <Card bodyless>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Post</th>
                    <th>Channel</th>
                    <th>Platform</th>
                    <th>State</th>
                    <th>Due</th>
                    <th className="num">Attempts</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((upload) => (
                    <Row key={upload.id} upload={upload}
                         onRetry={retry.run} busy={retry.busy} />
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
  upload,
  onRetry,
  busy,
}: {
  upload: UploadOut;
  onRetry: (id: string) => Promise<boolean>;
  busy: boolean;
}) {
  const stalled = upload.state === "failed" || upload.state === "needs_attention";
  const due = upload.next_attempt_at ?? upload.run_at;

  return (
    <tr>
      <td>
        <div className="truncate" style={{ fontWeight: 550 }}>
          {upload.title || <span className="faint">untitled</span>}
        </div>
        <div className="small faint mono">{upload.id}</div>
      </td>
      <td className="small">{upload.channel_name || upload.channel_id}</td>
      <td className="small">{upload.platform}</td>
      <td>
        <Pill state={upload.state} />
        {upload.last_error && (
          <div className="small" style={{ marginTop: 4, maxWidth: 300,
               color: stalled ? "var(--bad)" : "var(--text-dim)" }}>
            {upload.last_error}
          </div>
        )}
      </td>
      <td className="small" title={exact(due)}>{when(due)}</td>
      <td className="num small">{upload.attempt_count}</td>
      <td className="num">
        {/* Only offered where the API will accept it. Re-queueing something
            mid-flight is how the same video goes out twice. */}
        {stalled && (
          <button className="btn btn-sm" disabled={busy}
                  onClick={() => void onRetry(upload.id)}>
            Retry
          </button>
        )}
      </td>
    </tr>
  );
}
