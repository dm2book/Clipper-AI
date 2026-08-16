"""What a worker process has available, built once from the environment.

A handler asks for what it needs by name (`_service(context, "render_factory")`)
and gets `None` when the deployment does not have it. That is the shape on
purpose: a render box with no publishing credentials should run renders and
report `Fatal` on a publish job, rather than refusing to start or — far worse
— draining publish jobs into nowhere and marking them done.

The factories take `(database, tenant_id)` because every engine here is
tenant-scoped and a worker serves many tenants. Building one engine per
process and reusing it across tenants is how one customer's clip ends up in
another customer's workspace.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("clipforge.worker.services")

__all__ = ["WorkerServices", "services_from_env", "describe"]


@dataclass
class WorkerServices:
    """Handles a handler may ask for. `None` means "not on this worker"."""

    acquisition_factory: Callable[[Any, str], Any] | None = None
    transcription_factory: Callable[[Any, str], Any] | None = None
    render_factory: Callable[[Any, str], Any] | None = None
    analytics_factory: Callable[[Any, str], Any] | None = None
    publisher: Any = None
    transport: Any = None
    metric_source: Any = None
    face_tracker: Any = None
    storage: Any = None
    #: Why something is absent, keyed by service name. Reported by `describe`
    #: and by the monitoring endpoint, because "publish jobs keep dying" is
    #: answered by this dictionary and by nothing else.
    unavailable: dict[str, str] = field(default_factory=dict)

    def close(self) -> None:
        for held in (self.face_tracker, self.storage):
            closer = getattr(held, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:                           # noqa: BLE001
                    pass


def services_from_env(*, want_vision: bool = True) -> WorkerServices:
    """Assemble whatever this host can actually do.

    Nothing here raises. A worker that refuses to start because one optional
    dependency is missing is a worker that cannot run the six kinds of job it
    *could* have run, and the reason is recorded rather than fatal.
    """

    services = WorkerServices()

    # -- acquisition -------------------------------------------------------
    try:
        from ..acquire import AcquisitionEngine

        services.acquisition_factory = (
            lambda database, tenant: AcquisitionEngine(database, tenant)
        )
    except Exception as error:                              # noqa: BLE001
        services.unavailable["acquisition"] = f"{type(error).__name__}: {error}"

    # -- transcription -----------------------------------------------------
    try:
        from ..transcribe import TranscriptionEngine
        from ..transcribe.config import provider_from_env

        provider = provider_from_env()
        services.transcription_factory = (
            lambda database, tenant: TranscriptionEngine(
                database, tenant, provider=provider,
                storage=services.storage,
            )
        )
    except Exception as error:                              # noqa: BLE001
        services.unavailable["transcription"] = (
            f"no transcription provider is configured "
            f"({type(error).__name__}: {error})"
        )

    # -- storage -----------------------------------------------------------
    try:
        from ..storage.config import storage_from_env

        services.storage = storage_from_env()
    except Exception as error:                              # noqa: BLE001
        services.unavailable["storage"] = f"{type(error).__name__}: {error}"

    # -- render ------------------------------------------------------------
    ffmpeg = os.environ.get("CLIPFORGE_FFMPEG", "")
    try:
        import shutil

        from ..render import RenderConfig, RenderEngine

        binary = ffmpeg or shutil.which("ffmpeg") or ""
        if not binary:
            services.unavailable["render"] = (
                "ffmpeg is not on PATH and CLIPFORGE_FFMPEG is unset, so this "
                "worker cannot encode anything"
            )
        else:
            workspace = os.environ.get(
                "CLIPFORGE_RENDER_WORKSPACE", "/tmp/clipforge-renders"
            )
            os.makedirs(workspace, exist_ok=True)
            services.render_factory = (
                lambda database, tenant: RenderEngine(
                    database, tenant,
                    config=RenderConfig(workspace=workspace, ffmpeg=binary),
                    storage=services.storage,
                )
            )
    except Exception as error:                              # noqa: BLE001
        services.unavailable["render"] = f"{type(error).__name__}: {error}"

    # -- face detection ----------------------------------------------------
    if want_vision:
        try:
            from ..vision import FaceTrackEngine

            engine = FaceTrackEngine()
            available = engine.availability()
            if available.ready:
                services.face_tracker = engine
            else:
                services.unavailable["vision"] = available.detail
        except Exception as error:                          # noqa: BLE001
            services.unavailable["vision"] = f"{type(error).__name__}: {error}"

    # -- publishing --------------------------------------------------------
    try:
        from ..publish import PublishingSystem

        services.publisher = PublishingSystem()
    except Exception as error:                              # noqa: BLE001
        services.unavailable["publisher"] = f"{type(error).__name__}: {error}"

    if os.environ.get("CLIPFORGE_PUBLISH_TRANSPORT", "").lower() == "http":
        try:
            from ..publish import HttpTransport

            services.transport = HttpTransport()
        except Exception as error:                          # noqa: BLE001
            services.unavailable["transport"] = f"{type(error).__name__}: {error}"
    else:
        services.unavailable["transport"] = (
            "no upload transport configured. Set "
            "CLIPFORGE_PUBLISH_TRANSPORT=http to use the real HTTP client — "
            "without it, publish jobs fail rather than draining into nowhere."
        )

    # -- analytics ---------------------------------------------------------
    #
    # `AnalyticsEngine` takes `(config, store)`, not `(database, tenant)` —
    # its own constructor defaults to an in-memory `AnalyticsStore`, which
    # would silently lose every snapshot a worker collected. So the factory
    # binds `DurableAnalyticsStore` explicitly; the alternative is a
    # collection run that reports success and persists nothing.
    try:
        from ..analytics import AnalyticsEngine
        from ..store.durable import DurableAnalyticsStore

        services.analytics_factory = (
            lambda database, tenant: AnalyticsEngine(
                store=DurableAnalyticsStore(database, tenant)
            )
        )
    except Exception as error:                              # noqa: BLE001
        services.unavailable["analytics"] = f"{type(error).__name__}: {error}"

    services.unavailable.setdefault(
        "metric_source",
        "no live metric source exists in this build; RecordedSource is the "
        "only implementation, so collect_metrics jobs will fail rather than "
        "invent numbers",
    )
    return services


def describe(services: WorkerServices) -> dict[str, Any]:
    """A readable summary, for the log line a worker prints on startup."""

    return {
        "acquisition": services.acquisition_factory is not None,
        "transcription": services.transcription_factory is not None,
        "render": services.render_factory is not None,
        "publish": services.transport is not None,
        "analytics": services.metric_source is not None,
        "vision": services.face_tracker is not None,
        "storage": services.storage is not None,
        "unavailable": dict(services.unavailable),
    }
