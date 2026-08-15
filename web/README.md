# ClipForge AI — dashboard

React 18 + TypeScript + Vite. Seven pages, every one reading the real API.

```sh
npm install
npm run generate:api      # types, from the API's own OpenAPI document
npm run dev               # proxies /api to http://127.0.0.1:8000
npm run build             # tsc -b && vite build
```

## No mock data, and what that means here

There are no fixtures in this app and no `if (import.meta.env.DEV)` branches
feeding it sample rows. Every page renders what `/api/v1/...` returned, and
with the API stopped every page shows its error state rather than something
plausible.

`demo/seed_dashboard.py` exists and is not a contradiction: it writes rows into
PostgreSQL through the real stores, and the dashboard then reads them back
through the real API with no special casing. The distinction that matters is
where the data lives — a fixture in the frontend is a lie the UI tells itself.

## Types are generated, not written

`src/api/types.ts` comes from the API's OpenAPI document. Rename a field in
`src/clipforge/api/schemas.py`, regenerate, and every page that read the old
name fails `tsc` — instead of rendering `undefined` into a table cell where
nobody notices for a week.

Do not edit that file. It says so at the top.

## The three states every page has

`<Async>` takes a resource and will not render children until there is data,
so no page can forget loading, error or empty. Those three are most of what a
dashboard is and the first thing skipped when each page rolls its own.

Empty is deliberately distinct from error: a successful response carrying an
empty list is the state a new account spends its first day in, and it must not
look like a failure.

## Null is not zero

Metrics render `—` when the API returns null. That is not a formatting
preference: no live metric source is wired up, so most published posts have no
measurement, and drawing that as `0 views` would be a claim about the videos
rather than about the collection. The Published page says so in a banner and
Analytics refuses to chart an empty window.

## One refresh at a time

Access tokens live fifteen minutes, so long sessions hit a 401 mid-use. The
client refreshes once and retries, and concurrent 401s share a single in-flight
refresh. Without that, six components mounting together fire six refreshes; the
API rotates refresh tokens and treats a replayed one as theft, so five of them
would present a spent token and the whole session family would be revoked — a
page load that looks like an attack.

## Layout

```
src/
  api/
    types.ts      generated — do not edit
    client.ts     fetch, auth header, refresh-and-retry
    hooks.ts      useResource / useAction
    auth.tsx      who is signed in
  components/
    Shell.tsx     sidebar and page header
    ui.tsx        Card, Async, Pill, Empty, formatters
  pages/          one file per page, plus Login
```

## What it cannot do

Nothing here can publish a video, transcribe a source, or fetch a metric,
because the deployment behind it cannot either. The Settings page reports that
directly — object storage, live metrics, email delivery and the acquisition
worker all read *unavailable*, each with the reason. A dashboard that hid them
would show an upload queue that never drains and give no clue why.
