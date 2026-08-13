"""The layer that actually talks to the platforms.

Everything else in `publish/` builds requests and interprets responses without
performing I/O. This is the one module that opens a socket, and it is
deliberately the smallest interesting thing in the package: adapters own the
protocols, `retry.py` owns the policy, and this owns bytes on a wire.

## Why `http.client` and not `urllib`

`urllib.request` follows redirects and raises on 4xx, and both behaviours are
wrong here.

* **308 is not a redirect to follow.** Google's resumable protocol uses
  `308 Resume Incomplete` to mean "still going, here is how much I have" — the
  single most important status in the whole upload. A client that treats it as
  a redirect re-sends the chunk to a URL that was never meant to receive it.
* **4xx is data, not an exception.** `retry.py` classifies a 401 into REAUTH, a
  429 into a delay taken from `Retry-After`, and a 400 into a permanent
  failure. Turning those into exceptions means reconstructing the response
  from the exception to get the body back, and the body is where every
  platform puts the error code that decides the disposition.

So this speaks HTTP directly: exact status codes, no redirect handling, no
exceptions for anything the server actually said.

## Retries stop at the connection

There is a bounded retry here and it covers exactly one thing: failing to
establish a connection, before any byte of the request body has been sent.
That is safe because the platform has not been told anything.

Once bytes are on the wire, this layer gives up and reports. It does **not**
retry a failed upload, because it cannot know whether the platform processed
it — and `retry.py` can, using `already_in_flight`, which is the difference
between "retry" and "reconcile before sending anything else". A transport that
helpfully retried a POST would silently convert the ambiguous case into a
double post, which is the failure this system is most careful to avoid.

## Failures are typed by what a caller can do

* `TimeoutError` — the request took too long. The engine catches it and asks
  `retry.py`, which cares a great deal whether the platform had already been
  told something.
* `TransportError` — no usable reply: DNS, refused connection, TLS failure, a
  truncated response. Classified as a network failure and retried.

Anything the server said, however unwelcome, comes back as a `Response`.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import random
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .types import Request, Response

__all__ = [
    "HttpTransport",
    "TransportConfig",
    "TransportError",
    "DEFAULT_USER_AGENT",
]

DEFAULT_USER_AGENT = "ClipForge/0.1 (+https://clipforge.example)"

#: Bodies larger than this are not read into memory. Every response this
#: system expects is a small JSON document; a multi-megabyte reply means
#: something upstream is wrong — an HTML error page from a proxy, most often —
#: and reading it in full would turn that into a memory problem as well.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Read size when streaming an upload off disk. Independent of the platform's
#: chunk size: a 64 MB TikTok chunk is sent as a stream of these, so memory
#: stays flat no matter how the platform wants the file divided.
STREAM_BLOCK = 256 * 1024


class TransportError(Exception):
    """No usable reply came back.

    Distinct from a `Response` carrying a bad status, which means the platform
    answered and its answer decides what happens next.
    """


@dataclass(slots=True)
class TransportConfig:
    #: Establishing the TCP/TLS connection.
    connect_timeout_s: float = 15.0
    #: Waiting for a reply to an ordinary request.
    read_timeout_s: float = 60.0
    #: Waiting for a reply to a request that carries a chunk of video. Large,
    #: because the platform reads the whole chunk before answering and a slow
    #: link makes that minutes rather than seconds.
    upload_timeout_s: float = 900.0
    #: Attempts to *establish a connection*. Not attempts to send a request —
    #: see the module docstring.
    connect_attempts: int = 3
    connect_backoff_s: float = 0.5
    user_agent: str = DEFAULT_USER_AGENT
    #: PEM bundle. Defaults to the system store, then to the usual environment
    #: variables, so a corporate or sandbox CA works without code changes.
    ca_bundle: str = ""
    #: `http://host:port` for an egress proxy. Defaults to the environment.
    proxy_url: str = ""
    #: Hosts that bypass the proxy, comma separated. Defaults to the
    #: environment. `localhost` and loopback are always included, or the
    #: integration tests would try to reach their own server through a proxy.
    no_proxy: str = ""
    #: Called with `request.redacted()` before each send and with a small dict
    #: after. Redacted at the source rather than trusted to the caller: this
    #: is the one place where a bearer token is in a local variable, and a log
    #: line is how it escapes.
    observer: Callable[[str, dict[str, Any]], None] | None = None


class _RangeReader(io.RawIOBase):
    """A file-like view of one byte range, read straight off disk.

    `http.client` will pull from this in blocks once `Content-Length` is set,
    so a 64 MB chunk never exists in memory. The alternative — slicing the
    range into a `bytes` — makes peak memory the platform's chunk size times
    the number of workers, which is how a four-worker box dies on a long
    podcast.
    """

    def __init__(self, path: str, start: int, end: int) -> None:
        self._handle = open(path, "rb")  # noqa: SIM115 — closed in close()
        self._handle.seek(start)
        self._remaining = end - start + 1
        if self._remaining < 0:
            raise ValueError(f"empty byte range {start}-{end} for {path!r}")

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        block = self._handle.read(min(want, STREAM_BLOCK))
        if not block:
            # The file is shorter than the range claims. Silence here would
            # send a short body under an honest Content-Length and hang.
            raise TransportError(
                f"file ended {self._remaining} bytes before the range did — "
                f"the asset changed underneath the upload"
            )
        self._remaining -= len(block)
        return block

    def readinto(self, buffer: Any) -> int:
        block = self.read(len(buffer))
        buffer[: len(block)] = block
        return len(block)

    def close(self) -> None:
        try:
            self._handle.close()
        finally:
            super().close()


@dataclass
class HttpTransport:
    """A real HTTP client for the `Transport` protocol.

    Stateless between calls and safe to share across threads only in the sense
    that each `send` opens its own connection. Connection reuse is deliberately
    absent: uploads are minutes apart and megabytes each, so the handshake is
    noise, and a pooled connection that goes stale mid-upload costs far more
    than it saves.
    """

    config: TransportConfig = field(default_factory=TransportConfig)

    def __post_init__(self) -> None:
        self._ssl = self._build_ssl_context()

    # -- the protocol ------------------------------------------------------

    def send(self, request: Request) -> Response:
        body, length, stream = self._body(request)
        try:
            return self._send(request, body, length)
        finally:
            if stream is not None:
                stream.close()

    # -- request construction ----------------------------------------------

    def _body(self, request: Request) -> tuple[Any, int, _RangeReader | None]:
        """The body to send, its length, and anything needing closing."""

        if request.byte_range is not None:
            path = request.asset_path
            if not path:
                raise TransportError(
                    f"{request.description or request.url} asks for bytes "
                    f"{request.byte_range} but carries no asset path — the "
                    f"adapter must set Request.asset_path on chunk requests"
                )
            if not os.path.exists(path):
                raise TransportError(f"no media at {path!r} to upload")
            start, end = request.byte_range
            size = os.path.getsize(path)
            if end >= size:
                raise TransportError(
                    f"{path!r} is {size} bytes; the upload wants {start}-{end}. "
                    f"The asset changed after the upload was planned."
                )
            reader = _RangeReader(path, start, end)
            return reader, end - start + 1, reader

        if request.json_body is not None:
            raw = json.dumps(request.json_body).encode()
            return raw, len(raw), None

        if request.form_body is not None:
            raw = urllib.parse.urlencode(request.form_body).encode()
            return raw, len(raw), None

        return None, 0, None

    def _headers(self, request: Request, length: int) -> dict[str, str]:
        headers = dict(request.headers)
        headers.setdefault("User-Agent", self.config.user_agent)
        headers.setdefault("Accept", "application/json")

        if request.json_body is not None:
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
        elif request.form_body is not None:
            headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded"
            )

        # Computed, never inherited. An adapter's Content-Length is its
        # intent; this is what is actually going down the socket, and a
        # mismatch is a hang rather than an error.
        if length or request.method in ("POST", "PUT", "PATCH"):
            headers["Content-Length"] = str(length)
        return headers

    # -- the wire ----------------------------------------------------------

    def _send(self, request: Request, body: Any, length: int) -> Response:
        parsed = urllib.parse.urlsplit(request.url)
        if parsed.scheme not in ("http", "https"):
            raise TransportError(f"unsupported scheme in {request.url!r}")

        headers = self._headers(request, length)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        timeout = (
            self.config.upload_timeout_s
            if request.byte_range is not None
            else self.config.read_timeout_s
        )
        self._observe("request", request.redacted())

        connection = self._connect(parsed)
        started = time.monotonic()
        try:
            connection.timeout = timeout
            connection.sock.settimeout(timeout)
            connection.request(request.method, target, body=body, headers=headers)
            raw = connection.getresponse()
            payload = raw.read(MAX_RESPONSE_BYTES + 1)
        except socket.timeout as error:
            raise TimeoutError(
                f"{request.description or request.url} timed out after "
                f"{timeout:.0f}s"
            ) from error
        except (http.client.HTTPException, ssl.SSLError, OSError) as error:
            # A body that was partly sent lands here. There is nothing safe to
            # do about it at this layer — see the module docstring.
            raise TransportError(
                f"{request.description or request.url} failed: "
                f"{type(error).__name__}: {error}"
            ) from error
        finally:
            connection.close()

        if len(payload) > MAX_RESPONSE_BYTES:
            raise TransportError(
                f"{request.url} replied with more than "
                f"{MAX_RESPONSE_BYTES} bytes — not a platform API response"
            )

        response = Response(
            status=raw.status,
            headers={key.lower(): value for key, value in raw.getheaders()},
            body=_decode(payload),
        )
        self._observe("response", {
            "status": response.status,
            "elapsed_s": round(time.monotonic() - started, 3),
            "description": request.description,
            "bytes_sent": length,
        })
        return response

    def _connect(self, parsed: urllib.parse.SplitResult) -> http.client.HTTPConnection:
        """Open a connection, retrying only the connection itself."""

        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        proxy = self._proxy_for(host)

        last: Exception | None = None
        for attempt in range(max(1, self.config.connect_attempts)):
            try:
                return self._open(parsed, host, port, proxy)
            except (OSError, http.client.HTTPException) as error:
                last = error
                if attempt + 1 >= max(1, self.config.connect_attempts):
                    break
                # Full jitter. Two workers that lost the same flapping
                # upstream should not come back in lockstep.
                delay = self.config.connect_backoff_s * (2**attempt)
                time.sleep(random.uniform(0, delay))

        raise TransportError(
            f"could not connect to {host}:{port}"
            + (f" via proxy {proxy}" if proxy else "")
            + f" after {self.config.connect_attempts} attempts: {last}"
        )

    def _open(
        self,
        parsed: urllib.parse.SplitResult,
        host: str,
        port: int,
        proxy: str,
    ) -> http.client.HTTPConnection:
        secure = parsed.scheme == "https"
        timeout = self.config.connect_timeout_s

        if proxy:
            # Parsed, not split on the last colon: `http://host:port` has two
            # colons and the naive read leaves the scheme glued to the
            # hostname, which fails as a DNS lookup for "http://host".
            if "//" not in proxy:
                proxy = f"http://{proxy}"
            parts = urllib.parse.urlsplit(proxy)
            proxy_host = parts.hostname or ""
            proxy_port = parts.port or (443 if parts.scheme == "https" else 8080)

            # The class is chosen by the *target's* scheme, not the proxy's.
            # `HTTPSConnection.connect()` opens a plain socket to the proxy,
            # sends CONNECT, and only then wraps the tunnelled socket in TLS
            # with `server_hostname` set to the target. Using HTTPConnection
            # here because the proxy hop is plaintext would complete the
            # tunnel and then speak cleartext HTTP into it, which the far end
            # answers with a connection reset.
            connection: http.client.HTTPConnection = (
                http.client.HTTPSConnection(proxy_host, proxy_port,
                                            timeout=timeout, context=self._ssl)
                if secure
                else http.client.HTTPConnection(proxy_host, proxy_port,
                                                timeout=timeout)
            )
            # TLS stays end to end with the platform; the proxy sees an opaque
            # stream rather than the bearer token.
            connection.set_tunnel(host, port)
        elif secure:
            connection = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=self._ssl
            )
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)

        connection.connect()
        return connection

    # -- environment -------------------------------------------------------

    def _proxy_for(self, host: str) -> str:
        bypass = self.config.no_proxy or os.environ.get(
            "NO_PROXY", os.environ.get("no_proxy", "")
        )
        entries = [e.strip().lstrip("*.") for e in bypass.split(",") if e.strip()]
        # Loopback always bypasses. The integration tests run a real server on
        # 127.0.0.1, and routing that through an egress proxy is both wrong
        # and, in a sandbox, a 403.
        entries += ["localhost", "127.0.0.1", "::1"]
        lowered = host.lower()
        if any(lowered == e or lowered.endswith(f".{e}") for e in entries if e):
            return ""

        return self.config.proxy_url or os.environ.get(
            "HTTPS_PROXY", os.environ.get("https_proxy", "")
        )

    def _build_ssl_context(self) -> ssl.SSLContext:
        bundle = (
            self.config.ca_bundle
            or os.environ.get("SSL_CERT_FILE", "")
            or os.environ.get("REQUESTS_CA_BUNDLE", "")
        )
        context = ssl.create_default_context(cafile=bundle or None)
        # Stated rather than assumed. These are the defaults, and they are the
        # two lines somebody disables at 3am to make an upload work.
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def _observe(self, event: str, payload: dict[str, Any]) -> None:
        if self.config.observer is not None:
            self.config.observer(event, payload)


def _decode(payload: bytes) -> dict[str, Any]:
    """A JSON body, or a dict describing whatever arrived instead.

    Platforms answer with HTML when something in front of them fails — a proxy,
    a load balancer, a maintenance page. Returning `{}` there loses the only
    evidence of what went wrong, so the text is kept, truncated, under a key
    the error extractor already reads.
    """

    if not payload:
        return {}
    try:
        decoded = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        text = payload[:2000].decode("utf-8", "replace").strip()
        return {"error": {"message": f"non-JSON response: {text}"}}
    if isinstance(decoded, dict):
        return decoded
    return {"data": decoded}
