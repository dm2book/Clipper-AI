#!/usr/bin/env bash
#
# Start a worker.
#
#   scripts/run-worker.sh                       # every kind, every tenant
#   scripts/run-worker.sh --kinds render_video  # a render box
#   KINDS=publish_upload scripts/run-worker.sh  # same, from the environment
#
# `exec` matters: without it this shell stays as pid 1 under Docker or systemd
# and SIGTERM goes to bash, which does not forward it. The worker never hears
# the signal, never shuts down gracefully, and gets SIGKILLed at the end of the
# grace period with a job still leased.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}${here}/src"

: "${CLIPFORGE_WORKER_DSN:=${CLIPFORGE_DSN:-}}"
if [[ -z "${CLIPFORGE_WORKER_DSN}" && "${*}" != *"--in-memory"* ]]; then
  echo "Set CLIPFORGE_WORKER_DSN (or CLIPFORGE_DSN)." >&2
  echo "Prefer the worker role: its BYPASSRLS is scoped to \`jobs\`." >&2
  exit 2
fi
export CLIPFORGE_WORKER_DSN

args=()
[[ -n "${KINDS:-}" ]]   && args+=(--kinds ${KINDS})
[[ -n "${TENANTS:-}" ]] && args+=(--tenants ${TENANTS})
[[ -n "${LEASE:-}" ]]   && args+=(--lease "${LEASE}")

exec python3 -m clipforge.worker.main "${args[@]}" "$@"
