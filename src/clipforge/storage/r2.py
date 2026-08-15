"""Cloudflare R2, over boto3.

## boto3 rather than a hand-written signer

SigV4 is signed request canonicalisation, and getting it subtly wrong produces
a client that works until a key contains a space or a header arrives in a
different order. boto3 is the reference implementation Cloudflare's own R2
documentation points at, and it brings multipart, presigning, checksums and
connection reuse with it.

The HTTP transport in `publish/` is hand-written because it had to be — 308
resume and 4xx-as-data are wrong in every library. None of that applies here.

## What R2 is not

R2 is S3-compatible, not S3, and three differences matter:

* **`region_name` must be `auto`.** R2 has no regions. A real region is
  accepted by the signer and then rejected by the endpoint.
* **No storage classes and no `ACL`.** Passing `ACL="public-read"` fails.
  Public access is a bucket-level setting with a public domain in front, which
  is why `public_url` is configured rather than derived.
* **Lifecycle rules are set on the bucket, out of band.** `lifecycle.py`
  describes the rules this system wants and can apply them, but the day-to-day
  expiry is R2's job, not a sweeper's.

## Retries

boto3 has its own adaptive retry mode and it is left on. The loop here sits on
top and covers what botocore does not treat as retryable but which is, in
practice, worth one more attempt — a connection reset mid-upload most of all.
Reads are retried freely. Writes are retried too, and that is safe because
every write here is an idempotent PUT to a key the caller chose: repeating one
overwrites the same object with the same bytes.
"""

from __future__ import annotations

import contextlib
import mimetypes
import os
import random
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, BinaryIO, Iterator

from .protocol import StorageMetrics
from .types import (
    ObjectNotFound,
    PermanentStorageError,
    StoredObject,
    TransientStorageError,
    Visibility,
)

__all__ = ["R2Storage", "R2Config"]

#: Above this, boto3 switches to multipart automatically. 64 MB keeps a single
#: PUT within a sensible timeout on a slow uplink while avoiding the per-part
#: overhead on the many small files this system writes.
MULTIPART_THRESHOLD = 64 * 1024 * 1024
MULTIPART_CHUNK = 16 * 1024 * 1024

#: Codes worth another attempt. `RequestTimeout` and `InternalError` are the
#: common ones; the 5xx family is folded in by status.
_RETRYABLE_CODES = frozenset({
    "RequestTimeout", "RequestTimeoutException", "InternalError",
    "ServiceUnavailable", "SlowDown", "Throttling", "ThrottlingException",
    "RequestThrottled", "TooManyRequests", "ExpiredToken",
    "TransientError", "500", "502", "503", "504",
})
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})


@dataclass
class R2Config:
    bucket: str = ""
    account_id: str = ""
    #: Full endpoint. Derived from `account_id` when absent, which is the
    #: documented R2 form.
    endpoint_url: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    #: R2 has no regions and requires exactly this.
    region: str = "auto"
    #: The bucket's public domain, if one is configured — `https://media.x.com`
    #: or an `r2.dev` subdomain. Without it `public_url` raises, because a
    #: guessed URL that 403s is worse than an error that names the problem.
    public_base_url: str = ""
    #: Attempts on top of botocore's own. 1 means "no extra attempts".
    max_attempts: int = 4
    backoff_s: float = 0.4
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 120.0
    signed_url_ttl: timedelta = timedelta(hours=1)
    #: Sent as Cache-Control on public objects. Long, because a rendered clip
    #: never changes: the key contains the render id.
    public_cache_control: str = "public, max-age=31536000, immutable"

    def resolved_endpoint(self) -> str:
        if self.endpoint_url:
            return self.endpoint_url
        if not self.account_id:
            raise PermanentStorageError(
                "R2 needs either an endpoint URL or an account id"
            )
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


@dataclass
class R2Storage:
    """`Storage` over an S3-compatible endpoint."""

    config: R2Config
    metrics: StorageMetrics = field(default_factory=StorageMetrics)
    #: Injected by the tests so they can point at a local server without
    #: reaching for environment variables that would leak between cases.
    client: Any = None

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = self._build_client()

    def _build_client(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:                        # pragma: no cover
            raise PermanentStorageError(
                "boto3 is required for R2 — `pip install 'clipforge[storage]'`"
            ) from error

        if not self.config.access_key_id or not self.config.secret_access_key:
            raise PermanentStorageError(
                "R2 credentials are missing. Set CLIPFORGE_R2_ACCESS_KEY_ID "
                "and CLIPFORGE_R2_SECRET_ACCESS_KEY; there is deliberately no "
                "fallback to a local directory, because a deployment that "
                "silently writes media to a container's disk loses it on the "
                "next deploy."
            )

        return boto3.client(
            "s3",
            endpoint_url=self.config.resolved_endpoint(),
            aws_access_key_id=self.config.access_key_id,
            aws_secret_access_key=self.config.secret_access_key,
            region_name=self.config.region,
            config=Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=self.config.connect_timeout_s,
                read_timeout=self.config.read_timeout_s,
                # R2 rejects the newer default checksum headers on some paths;
                # requesting them only when asked keeps PUTs compatible.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                signature_version="s3v4",
            ),
        )

    @property
    def backend(self) -> str:
        return "r2"

    # -- the retry loop ----------------------------------------------------

    def _call(self, operation: str, action, *, size: int = 0):
        """Run one storage call, with retries and metrics around it."""

        started = time.monotonic()
        retries = 0
        last: Exception | None = None

        for attempt in range(max(1, self.config.max_attempts)):
            try:
                result = action()
            except Exception as error:                      # noqa: BLE001
                translated = _translate(error)
                if isinstance(translated, PermanentStorageError):
                    self.metrics.record(
                        operation, seconds=time.monotonic() - started,
                        failed=True, retries=retries,
                    )
                    raise translated from error
                last = translated
                if attempt + 1 >= max(1, self.config.max_attempts):
                    break
                retries += 1
                # Full jitter: several workers that lost the same endpoint
                # should not come back in lockstep and lose it again.
                time.sleep(random.uniform(0, self.config.backoff_s * 2**attempt))
                continue

            self.metrics.record(
                operation, seconds=time.monotonic() - started, size=size,
                retries=retries,
            )
            return result

        self.metrics.record(
            operation, seconds=time.monotonic() - started, failed=True,
            retries=retries,
        )
        raise TransientStorageError(
            f"{operation} failed after {self.config.max_attempts} attempts: "
            f"{last}"
        ) from last

    # -- writing -----------------------------------------------------------

    def put_file(
        self, key: str, path: str, *, content_type: str = "",
        visibility: Visibility = Visibility.PRIVATE,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        if not os.path.exists(path):
            raise PermanentStorageError(f"nothing to upload at {path!r}")
        size = os.path.getsize(path)

        extra: dict[str, Any] = {
            "ContentType": content_type or _guess_type(path),
        }
        if metadata:
            extra["Metadata"] = {k: str(v)[:512] for k, v in metadata.items()}
        if visibility is Visibility.PUBLIC:
            # Not an ACL: R2 rejects those. Public access is a bucket setting;
            # this only makes the CDN in front of it cache properly.
            extra["CacheControl"] = self.config.public_cache_control

        from boto3.s3.transfer import TransferConfig

        transfer = TransferConfig(
            multipart_threshold=MULTIPART_THRESHOLD,
            multipart_chunksize=MULTIPART_CHUNK,
            use_threads=True,
        )
        self._call(
            "put_file",
            lambda: self.client.upload_file(
                path, self.config.bucket, key, ExtraArgs=extra,
                Config=transfer,
            ),
            size=size,
        )
        return self.stat(key)

    # -- reading -----------------------------------------------------------

    def get_file(self, key: str, path: str) -> StoredObject:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # Downloaded to a sibling temp file and renamed, so a failure halfway
        # leaves nothing that looks like a complete file. ffmpeg reading a
        # truncated download fails in ways that get blamed on the encoder.
        scratch = f"{path}.part"
        try:
            self._call(
                "get_file",
                lambda: self.client.download_file(
                    self.config.bucket, key, scratch
                ),
            )
            os.replace(scratch, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(scratch)
            raise
        stored = self.stat(key)
        self.metrics.record("get_file_bytes", size=stored.size_bytes)
        return stored

    def open(self, key: str) -> BinaryIO:
        """A streaming reader. The caller closes it."""

        response = self._call(
            "open",
            lambda: self.client.get_object(Bucket=self.config.bucket, Key=key),
        )
        return response["Body"]

    def stat(self, key: str) -> StoredObject:
        head = self._call(
            "stat",
            lambda: self.client.head_object(Bucket=self.config.bucket, Key=key),
        )
        return StoredObject(
            key=key,
            size_bytes=int(head.get("ContentLength", 0)),
            etag=str(head.get("ETag", "")).strip('"'),
            content_type=head.get("ContentType", ""),
            modified_at=head.get("LastModified"),
            metadata=dict(head.get("Metadata", {})),
        )

    def exists(self, key: str) -> bool:
        try:
            self.stat(key)
        except ObjectNotFound:
            return False
        return True

    # -- deleting ----------------------------------------------------------

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        self._call(
            "delete",
            lambda: self.client.delete_object(
                Bucket=self.config.bucket, Key=key
            ),
        )
        return existed

    def delete_prefix(self, prefix: str) -> int:
        """Delete everything under a prefix, in batches.

        The batch form matters: a tenant deletion is tens of thousands of
        objects, and one request each is both slow and a way to be throttled
        into a partial delete that looks complete.
        """

        if not prefix:
            raise PermanentStorageError(
                "refusing to delete an empty prefix — that is the whole bucket"
            )
        removed = 0
        batch: list[dict[str, str]] = []

        for stored in self.list(prefix, limit=0):
            batch.append({"Key": stored.key})
            if len(batch) == 1000:
                removed += self._delete_batch(batch)
                batch = []
        if batch:
            removed += self._delete_batch(batch)
        return removed

    def _delete_batch(self, batch: list[dict[str, str]]) -> int:
        self._call(
            "delete_batch",
            lambda: self.client.delete_objects(
                Bucket=self.config.bucket,
                Delete={"Objects": batch, "Quiet": True},
            ),
        )
        return len(batch)

    # -- listing -----------------------------------------------------------

    def list(self, prefix: str, limit: int = 1000) -> Iterator[StoredObject]:
        paginator = self.client.get_paginator("list_objects_v2")
        seen = 0
        for page in paginator.paginate(
            Bucket=self.config.bucket, Prefix=prefix
        ):
            for item in page.get("Contents", []):
                yield StoredObject(
                    key=item["Key"],
                    size_bytes=int(item.get("Size", 0)),
                    etag=str(item.get("ETag", "")).strip('"'),
                    modified_at=item.get("LastModified"),
                )
                seen += 1
                if limit and seen >= limit:
                    return

    # -- URLs --------------------------------------------------------------

    def signed_url(
        self, key: str, *, expires_in: timedelta | None = None,
        download_as: str = "",
    ) -> str:
        params: dict[str, Any] = {"Bucket": self.config.bucket, "Key": key}
        if download_as:
            params["ResponseContentDisposition"] = (
                f'attachment; filename="{download_as}"'
            )
        ttl = int((expires_in or self.config.signed_url_ttl).total_seconds())
        return self._call(
            "signed_url",
            lambda: self.client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=ttl,
            ),
        )

    def signed_upload_url(
        self, key: str, *, expires_in: timedelta | None = None,
        content_type: str = "",
    ) -> str:
        """A URL a browser can PUT to directly.

        The point is that the file never passes through the API. A 2 GB upload
        proxied through a request handler occupies a worker for the whole
        transfer and is the easiest way to take the API down with one user.
        """

        params: dict[str, Any] = {"Bucket": self.config.bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        ttl = int((expires_in or self.config.signed_url_ttl).total_seconds())
        return self._call(
            "signed_upload_url",
            lambda: self.client.generate_presigned_url(
                "put_object", Params=params, ExpiresIn=ttl,
            ),
        )

    def public_url(self, key: str) -> str:
        """An unsigned URL, for Instagram.

        Raises when no public domain is configured rather than guessing one.
        A guessed URL that 403s surfaces as "Instagram could not fetch the
        media", which sends the next person to debug Meta's API instead of
        this setting.
        """

        if not self.config.public_base_url:
            raise PermanentStorageError(
                "no public base URL is configured, so this object has no "
                "unsigned URL. Instagram fetches media itself and cannot "
                "present a signature, so Reels cannot publish until the "
                "bucket has a public domain and CLIPFORGE_R2_PUBLIC_BASE_URL "
                "names it."
            )
        return f"{self.config.public_base_url.rstrip('/')}/{key}"

    # -- metrics -----------------------------------------------------------

    def usage(self, prefix: str = "") -> dict[str, Any]:
        """Bytes and object count under a prefix.

        A full listing, so it is not something to call on every request. R2
        exposes no cheap size API; the honest options are this or a counter
        maintained on every write, and a counter that drifts is worse than a
        number that costs a listing.
        """

        total = 0
        count = 0
        largest: StoredObject | None = None
        for stored in self.list(prefix, limit=0):
            total += stored.size_bytes
            count += 1
            if largest is None or stored.size_bytes > largest.size_bytes:
                largest = stored
        return {
            "prefix": prefix,
            "objects": count,
            "bytes": total,
            "gigabytes": round(total / 1e9, 3),
            "largest": largest.to_dict() if largest else None,
            "backend": self.backend,
        }


def _guess_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _translate(error: Exception) -> Exception:
    """Map a botocore error onto something a caller can act on."""

    try:
        from botocore.exceptions import (
            ClientError, ConnectionError as BotoConnectionError,
            EndpointConnectionError, ReadTimeoutError,
        )
    except ImportError:                                     # pragma: no cover
        return TransientStorageError(str(error))

    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        status = str(
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", "")
        )
        if code in _NOT_FOUND_CODES or status == "404":
            return ObjectNotFound(f"no object: {code or status}")
        if code in _RETRYABLE_CODES or status in _RETRYABLE_CODES:
            return TransientStorageError(f"{code or status}: {error}")
        # 403 is almost always credentials or a bucket policy, and retrying a
        # bad key for four attempts just delays the useful error.
        return PermanentStorageError(f"{code or status}: {error}")

    if isinstance(error, (EndpointConnectionError, BotoConnectionError,
                          ReadTimeoutError, TimeoutError, ConnectionError)):
        return TransientStorageError(str(error))
    if isinstance(error, (FileNotFoundError, IsADirectoryError)):
        return PermanentStorageError(str(error))
    return TransientStorageError(f"{type(error).__name__}: {error}")
