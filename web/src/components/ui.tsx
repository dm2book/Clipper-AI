/**
 * The shared pieces. Small, and deliberately not a component library.
 *
 * `Async` is the important one: it takes a `Resource` and refuses to render
 * children until there is data, so no page can forget the loading, error or
 * empty case. Those three are most of what a dashboard is, and they are what
 * gets skipped when each page rolls its own.
 */
import type { ReactNode } from "react";
import type { Resource } from "../api/hooks";

export function Card({
  title,
  action,
  children,
  bodyless,
}: {
  title?: string;
  action?: ReactNode;
  children: ReactNode;
  bodyless?: boolean;
}) {
  return (
    <section className="card">
      {title && (
        <header className="card-head">
          <span className="card-title">{title}</span>
          {action}
        </header>
      )}
      {bodyless ? children : <div className="card-body">{children}</div>}
    </section>
  );
}

export function Empty({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {detail && <p className="empty-detail">{detail}</p>}
    </div>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "bad";
  children: ReactNode;
}) {
  const cls = tone === "bad" ? "notice notice-bad" : tone === "warn" ? "notice notice-warn" : "notice";
  return <div className={cls}>{children}</div>;
}

export function Skeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="stack" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ width: `${94 - i * 7}%` }} />
      ))}
    </div>
  );
}

/**
 * Render `children` only once data exists.
 *
 * `isEmpty` is separate from `data === null` on purpose: a successful response
 * carrying an empty list is not an error and must not look like one, and it is
 * the state a new account spends its first day in.
 */
export function Async<T>({
  resource,
  children,
  empty,
  skeletonRows,
}: {
  resource: Resource<T>;
  children: (data: T) => ReactNode;
  empty?: { title: string; detail?: string; when: (data: T) => boolean };
  skeletonRows?: number;
}) {
  const { data, error, loading, reload } = resource;

  if (error) {
    return (
      <Notice tone="bad">
        <div>
          <strong>{error.message}</strong>
          <div className="small" style={{ marginTop: 6 }}>
            <button className="btn btn-sm" onClick={reload}>
              Try again
            </button>
            <span className="faint mono" style={{ marginLeft: 8 }}>
              {error.code}
            </span>
          </div>
        </div>
      </Notice>
    );
  }
  if (loading && data === null) return <Skeleton rows={skeletonRows} />;
  if (data === null) return <Empty title="Nothing to show" />;
  if (empty?.when(data)) return <Empty title={empty.title} detail={empty.detail} />;
  return <>{children(data)}</>;
}

/* -- formatting -------------------------------------------------------- */

/** Null renders as an em dash, never as zero. The distinction is load-bearing
 *  everywhere metrics appear: "not measured" is not "no views". */
export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function money(cents: number): string {
  return (cents / 100).toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

export function duration(seconds: number): string {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const at = new Date(iso);
  const diff = (Date.now() - at.getTime()) / 1000;
  const ahead = diff < 0;
  const seconds = Math.abs(diff);
  const say = (n: number, unit: string) =>
    ahead ? `in ${n}${unit}` : `${n}${unit} ago`;

  if (seconds < 60) return ahead ? "shortly" : "just now";
  if (seconds < 3600) return say(Math.round(seconds / 60), "m");
  if (seconds < 86400) return say(Math.round(seconds / 3600), "h");
  if (seconds < 86400 * 30) return say(Math.round(seconds / 86400), "d");
  return at.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function exact(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "";
}

/* -- state pills -------------------------------------------------------- */

const TONES: Record<string, string> = {
  active: "pill-ok", published: "pill-ok", ready: "pill-ok",
  succeeded: "pill-ok", complete: "pill-ok", owned: "pill-ok",
  licensed: "pill-ok",

  scheduled: "pill-info", processing: "pill-info", uploading: "pill-info",
  queued: "pill-info", draft: "pill",

  paused: "pill-warn", retrying: "pill-warn", awaiting_creator: "pill-warn",
  needs_attention: "pill-warn", failed_retryable: "pill-warn",
  unverified: "pill-warn",

  failed: "pill-bad", circuit_open: "pill-bad", dead: "pill-bad",
  failed_permanent: "pill-bad", cancelled: "pill-bad",
};

export function Pill({ state, label }: { state?: string; label?: string }) {
  if (!state) return <span className="faint">—</span>;
  const cls = TONES[state] ?? "pill";
  return (
    <span className={`pill ${cls}`}>
      <span className="pill-dot" />
      {label ?? state.replace(/_/g, " ")}
    </span>
  );
}

export function Bar({ value, total, tone }: { value: number; total: number; tone?: string }) {
  const pct = total > 0 ? Math.min(100, (value / total) * 100) : 0;
  return (
    <div className="bar" role="img" aria-label={`${value} of ${total}`}>
      <div className={`bar-fill ${tone ?? ""}`} style={{ width: `${pct}%` }} />
    </div>
  );
}
