"""ClipForge AI — durable media storage.

    from clipforge.storage import Workspace, storage_from_env

    storage = storage_from_env()
    with Workspace(storage, tenant_id) as work:
        media = work.fetch(source_ref)          # object → scratch file
        output = work.path("clip.mp4")
        ...ffmpeg...
        ref = work.publish(output, work.key("renders", clip_id, "clip.mp4"))

## R2 is where media lives; local disk is scratch

The migration is not "delete every local path", because ffmpeg reads files and
yt-dlp writes them and neither speaks S3. It is a change of *system of record*:
the durable copy is an object, a working copy exists for as long as a job is
running, and `Workspace` is the only place the two meet. Anything still on
local disk after a job finishes is a bug, and `sweep()` finds it.

## Keys are tenant-scoped, and `key_for` is the only way to build one

`ten_acme/renders/cl_123/clip.mp4`. A bug that builds a wrong key lands inside
the same tenant's prefix, an IAM policy can be scoped by prefix, and deleting a
customer is a prefix delete. `key_for` refuses traversal, because `a/../b` is a
different object from `b` in an object store rather than the same one.

## What is verified, and what is not

`tests/test_storage.py` runs one contract over both backends, and the R2 side
runs against a real S3-compatible server (moto) over real HTTP with real boto3:
uploads, multipart, ranges, presigned URLs, listing, prefix deletes, lifecycle
configuration and the retry loop.

**No byte has reached Cloudflare.** `*.r2.cloudflarestorage.com` is refused by
this environment's egress policy and there are no R2 credentials, so what is
proven is that this client is correct against the S3 API — not that R2 behaves
as documented. `describe_environment()` reports whether real credentials are
configured, and the three R2-specific departures from S3 are handled
explicitly: `region="auto"`, no ACLs, and a public domain that must be
configured rather than derived.
"""

from .config import (
    BACKENDS,
    ENV_PREFIX,
    describe_environment,
    r2_config_from_env,
    storage_from_env,
)
from .lifecycle import RULES, Rule, plan_prune
from .local import LocalStorage
from .protocol import OperationStats, Storage, StorageMetrics
from .r2 import R2Config, R2Storage
from .types import (
    ObjectNotFound,
    PermanentStorageError,
    StorageError,
    StorageRef,
    StoredObject,
    TransientStorageError,
    Visibility,
    key_for,
)
from .workspace import Workspace, sweep

__all__ = [
    "BACKENDS",
    "ENV_PREFIX",
    "LocalStorage",
    "ObjectNotFound",
    "OperationStats",
    "PermanentStorageError",
    "R2Config",
    "R2Storage",
    "RULES",
    "Rule",
    "Storage",
    "StorageError",
    "StorageMetrics",
    "StorageRef",
    "StoredObject",
    "TransientStorageError",
    "Visibility",
    "Workspace",
    "describe_environment",
    "key_for",
    "plan_prune",
    "r2_config_from_env",
    "storage_from_env",
    "sweep",
]
