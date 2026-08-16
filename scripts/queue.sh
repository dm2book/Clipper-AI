#!/usr/bin/env bash
#
# Look at the queue without starting a worker.
#
#   scripts/queue.sh                     # JSON snapshot per tenant
#   scripts/queue.sh --requeue-dead      # empty the dead-letter state
#
# Exits non-zero when a queue is unhealthy — stale leases, or an oldest job
# older than an hour — so it works as a cron check as well as by hand.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}${here}/src"
: "${CLIPFORGE_WORKER_DSN:=${CLIPFORGE_DSN:-}}"
export CLIPFORGE_WORKER_DSN

if [[ "${*}" == *"--requeue-dead"* ]]; then
  exec python3 -m clipforge.worker.main "$@"
fi
exec python3 -m clipforge.worker.main --queue "$@"
