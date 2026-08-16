#!/usr/bin/env bash
#
# Start the API.
#
#   scripts/run-api.sh
#   scripts/run-api.sh --in-memory      # nothing is persisted
#
# The readiness gate in `clipforge.api.server` refuses to start on an unsafe
# configuration — a generated JWT key, unverified sign-in, weak Argon2
# parameters. That is deliberate and this script does not bypass it; pass
# --skip-readiness-check yourself if you mean to.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}${here}/src"

exec python3 -m clipforge.api.server "$@"
