"""The runnable process.

    CLIPFORGE_DSN=postgresql://clipforge_app:...@host/clipforge \
    CLIPFORGE_AUTH_DSN=postgresql://clipforge_auth:...@host/clipforge \
    CLIPFORGE_AUTH_SIGNING_KEYS=k1:$(openssl rand -base64 48) \
      python -m clipforge.api.server

Until this file existed the repository had no entrypoint at all: no
`console_scripts`, no `__main__`, nothing that could be started. Every layer
was importable and none was runnable.

`--in-memory` gives a working API with nothing persisted, for a first look and
for the dashboard's own development. It has to be asked for, because the
alternative — falling back to it when a DSN is missing — is a production
deployment that starts cleanly, serves requests, and loses everything on
restart with no error anywhere.
"""

from __future__ import annotations

import argparse
import os
import sys

from .app import build_services, create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ClipForge API.")
    parser.add_argument("--host", default=os.environ.get("CLIPFORGE_API_HOST",
                                                         "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("CLIPFORGE_API_PORT", "8000")))
    parser.add_argument("--in-memory", action="store_true",
                        help="run with no database; nothing is persisted")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--skip-readiness-check", action="store_true",
                        help="start even if the auth configuration is unsafe")
    args = parser.parse_args(argv)

    if not args.in_memory and not args.skip_readiness_check:
        from ..auth.config import MisconfiguredAuth, config_from_env

        try:
            config_from_env().require_production_ready()
        except MisconfiguredAuth as error:
            print(f"\nRefusing to start.\n\n{error}\n", file=sys.stderr)
            print(
                "Fix the configuration, or pass --skip-readiness-check if you "
                "know what you are doing.\n", file=sys.stderr,
            )
            return 2

    services = build_services(in_memory=args.in_memory)
    app = create_app(services)

    if args.in_memory:
        print("\n  Running with in-memory storage. Nothing is persisted.\n",
              file=sys.stderr)

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        services.close()
    return 0


#: For `uvicorn clipforge.api.server:app`. Built lazily so importing this
#: module for `main()` does not open a connection pool as a side effect.
def __getattr__(name: str):
    if name == "app":
        return create_app(build_services())
    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())
