"""The worker process.

    clipforge-worker --tenants ten_acme
    clipforge-worker --kinds render_video --lease 900
    clipforge-worker --queue                       # print the queue and exit
    clipforge-worker --requeue-dead render_video   # empty the dead letters

Deliberately a separate process from the API. They fail differently and scale
differently: an API pod is memory-light and latency-sensitive, a render worker
holds a gigabyte of ffmpeg and does not care about a hundred milliseconds. Put
them together and one bad render pauses every request on the box.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Sequence

from .handlers import default_handlers
from .monitor import requeue_dead, snapshot
from .runtime import Worker, WorkerConfig
from .services import describe, services_from_env

log = logging.getLogger("clipforge.worker")


def _database(dsn: str, in_memory: bool):
    if in_memory:
        from ..store import MemoryDatabase

        return MemoryDatabase()
    from ..store.postgres import PostgresDatabase

    resolved = dsn or os.environ.get("CLIPFORGE_WORKER_DSN") or os.environ.get(
        "CLIPFORGE_DSN", ""
    )
    if not resolved:
        raise SystemExit(
            "No database. Set CLIPFORGE_WORKER_DSN (preferred — it connects "
            "as clipforge_worker, whose BYPASSRLS is scoped to `jobs`) or "
            "CLIPFORGE_DSN, or pass --in-memory for a throwaway run."
        )
    return PostgresDatabase(resolved)


def _tenants(database, named: Sequence[str]) -> list[str]:
    """Which tenants to poll.

    Named ones win. Otherwise the worker asks the database — and if it cannot,
    it says so rather than silently polling nothing, which is the failure that
    looks exactly like an empty queue.
    """

    if named:
        return list(named)
    env = os.environ.get("CLIPFORGE_WORKER_TENANTS", "").strip()
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    lister = getattr(database, "tenant_ids", None)
    if lister is not None:
        try:
            return list(lister())
        except Exception:                                   # noqa: BLE001
            log.exception("could not list tenants")
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipforge-worker", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dsn", default="")
    parser.add_argument("--in-memory", action="store_true",
                        help="throwaway store; nothing is persisted")
    parser.add_argument("--tenants", nargs="*", default=(),
                        help="tenant ids to serve; default is all of them")
    parser.add_argument("--kinds", nargs="*", default=(),
                        help="job kinds to claim; default is all of them")
    parser.add_argument("--lease", type=int, default=300,
                        help="lease seconds (default 300)")
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--idle-sleep", type=float, default=1.0)
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="stop after this many jobs; 0 means never")
    parser.add_argument("--max-seconds", type=float, default=0.0)
    parser.add_argument("--name", default="",
                        help="lease owner id; default is host:pid:random")
    parser.add_argument("--no-vision", action="store_true",
                        help="skip loading the face model")
    parser.add_argument("--queue", action="store_true",
                        help="print a queue snapshot and exit")
    parser.add_argument("--requeue-dead", metavar="KIND", nargs="?",
                        const="", default=None,
                        help="move dead jobs back to queued and exit")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    database = _database(args.dsn, args.in_memory)
    tenants = _tenants(database, args.tenants)

    if args.queue:
        if not tenants:
            print("No tenants. Pass --tenants or set "
                  "CLIPFORGE_WORKER_TENANTS.", file=sys.stderr)
            return 2
        payload = [snapshot(database, t).to_dict() for t in tenants]
        print(json.dumps(payload, indent=2))
        return 0 if all(p["healthy"] for p in payload) else 1

    if args.requeue_dead is not None:
        total = sum(
            requeue_dead(database, tenant, kind=args.requeue_dead)
            for tenant in tenants
        )
        print(f"requeued {total} dead job(s)")
        return 0

    services = services_from_env(want_vision=not args.no_vision)
    capability = describe(services)
    log.info("capabilities: %s", json.dumps(capability))
    for name, reason in sorted(services.unavailable.items()):
        log.warning("%s unavailable: %s", name, reason)

    if not tenants:
        log.error(
            "no tenants to serve — the worker would poll nothing and look "
            "healthy. Pass --tenants or set CLIPFORGE_WORKER_TENANTS."
        )
        return 2

    worker = Worker(
        database,
        default_handlers(),
        WorkerConfig(
            kinds=tuple(args.kinds),
            lease_s=args.lease,
            heartbeat_s=max(5.0, args.lease / 3),
            idle_sleep_s=args.idle_sleep,
            batch=args.batch,
            max_jobs=args.max_jobs,
            max_seconds=args.max_seconds,
            tenants=tuple(tenants),
            services=services,
            name=args.name,
        ),
    )
    worker.install_signal_handlers()

    try:
        stats = worker.run()
    finally:
        services.close()
        closer = getattr(database, "close", None)
        if closer is not None:
            closer()

    log.info("final: %s", json.dumps(stats.to_dict()))
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
