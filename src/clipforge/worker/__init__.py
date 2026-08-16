"""The process that turns the queue into work being done.

`jobs` already had everything a durable queue needs — leases, attempt counting
in SQL, dedupe keys, `FOR UPDATE SKIP LOCKED`. What it did not have was
anything that called it. The audit's four findings were one finding: no worker
process existed, so no queue was drained, so nothing rendered and nothing
published.

```python
from clipforge.worker import Worker, WorkerConfig, default_handlers
from clipforge.worker.services import services_from_env

worker = Worker(database, default_handlers(), WorkerConfig(
    tenants=("ten_acme",), services=services_from_env(),
))
worker.install_signal_handlers()
worker.run()
```

Or `clipforge-worker --tenants ten_acme`.

| Module | Responsibility |
|---|---|
| `types.py` | What a handler is given and may answer. |
| `runtime.py` | Claim, heartbeat, retry, reap, shut down cleanly. |
| `handlers.py` | The five kinds, over the engines that already existed. |
| `services.py` | What this host can actually do, and why not otherwise. |
| `monitor.py` | Depth, oldest, dead letters, stale leases. |
| `main.py` | The entrypoint. |
"""

from .handlers import (
    acquisition_handler,
    analytics_handler,
    default_handlers,
    publish_handler,
    render_handler,
    transcription_handler,
)
from .monitor import KindDepth, QueueSnapshot, requeue_dead, snapshot
from .runtime import Worker, WorkerConfig, backoff_seconds
from .services import WorkerServices, describe, services_from_env
from .types import (
    Disposition,
    Done,
    Fatal,
    Handler,
    JobContext,
    Outcome,
    Retry,
    WorkerStats,
)

__all__ = [
    "Disposition",
    "Done",
    "Fatal",
    "Handler",
    "JobContext",
    "KindDepth",
    "Outcome",
    "QueueSnapshot",
    "Retry",
    "Worker",
    "WorkerConfig",
    "WorkerServices",
    "WorkerStats",
    "acquisition_handler",
    "analytics_handler",
    "backoff_seconds",
    "default_handlers",
    "describe",
    "publish_handler",
    "render_handler",
    "requeue_dead",
    "services_from_env",
    "snapshot",
    "transcription_handler",
]
