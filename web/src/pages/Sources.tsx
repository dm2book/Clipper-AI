import { useState } from "react";
import { PageHead } from "../components/Shell";
import { query } from "../api/client";
import { useResource } from "../api/hooks";
import { Async, Card, Pill, duration, num, when } from "../components/ui";
import type { PageSourceOut } from "../api/types";

export function Sources() {
  const [q, setQ] = useState("");
  const [transcribed, setTranscribed] = useState("");

  const sources = useResource<PageSourceOut>(
    `/sources${query({
      q,
      transcribed: transcribed === "" ? undefined : transcribed === "yes",
      limit: 100,
    })}`,
  );

  return (
    <>
      <PageHead
        title="Sources"
        sub="The long-form material, and how far each piece has got through acquisition and transcription."
      />

      <div className="toolbar">
        <input
          placeholder="Search title, creator or URL"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ minWidth: 260 }}
          aria-label="Search sources"
        />
        <label htmlFor="tr">Transcript</label>
        <select id="tr" value={transcribed} onChange={(e) => setTranscribed(e.target.value)}>
          <option value="">any</option>
          <option value="yes">yes</option>
          <option value="no">no</option>
        </select>
        <span className="spacer" />
        {sources.data && (
          <span className="small faint">
            {sources.data.items.length} of {sources.data.total}
          </span>
        )}
      </div>

      <Async
        resource={sources}
        empty={{
          title: q || transcribed ? "Nothing matches" : "No sources yet",
          detail:
            q || transcribed
              ? "Try a broader search."
              : "Sources arrive from a YouTube URL, a channel, a podcast feed or an uploaded file.",
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
                    <th>Kind</th>
                    <th className="num">Length</th>
                    <th>Acquisition</th>
                    <th>Transcript</th>
                    <th>Rights</th>
                    <th>Added</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((source) => (
                    <tr key={source.id}>
                      <td>
                        <div className="truncate" style={{ fontWeight: 550 }}>
                          {source.title}
                        </div>
                        <div className="small faint">
                          {source.creator || "unknown creator"}
                          {source.language && ` · ${source.language}`}
                        </div>
                      </td>
                      <td className="small">{source.kind.replace(/_/g, " ")}</td>
                      <td className="num small">{duration(source.duration_s)}</td>
                      <td><Pill state={source.acquisition_state} /></td>
                      <td>
                        {source.has_transcript ? (
                          <>
                            <Pill state="succeeded" label="transcribed" />
                            {(source.word_count ?? 0) > 0 && (
                              <div className="small faint" style={{ marginTop: 3 }}>
                                {num(source.word_count)} words
                              </div>
                            )}
                          </>
                        ) : source.transcription_state ? (
                          <Pill state={source.transcription_state} />
                        ) : (
                          <span className="faint small">not started</span>
                        )}
                      </td>
                      <td>
                        <Pill state={source.rights_basis} />
                        {source.rights_expires_at && (
                          <div className="small" style={{ color: "var(--warn)", marginTop: 3 }}>
                            expires {when(source.rights_expires_at)}
                          </div>
                        )}
                      </td>
                      <td className="small faint">{when(source.created_at)}</td>
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
