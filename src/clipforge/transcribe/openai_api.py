"""Transcription over an OpenAI-compatible HTTP API.

`POST /v1/audio/transcriptions`, multipart, `response_format=verbose_json`
with `timestamp_granularities[]=word` — the shape OpenAI documents and that
Groq, Together, a local `whisper.cpp` server, faster-whisper-server and
LocalAI all reimplement. Pointing `CLIPFORGE_TRANSCRIBE_BASE_URL` at any of
them is the whole configuration.

## Status in this repository

**The client is real; no OpenAI endpoint has been reached from here.** The
environment blocks outbound hosts other than package registries. What *is*
tested, over a real socket, is this client against a real local HTTP server
implementing the documented protocol: the multipart body it builds, the
headers it sends, the `verbose_json` it parses, how it classifies 401 against
429 against 500, and what it does on a timeout.

That verifies this code. It does not verify that OpenAI's service behaves as
documented, and no test here should be read as evidence that it does.

## No key is ever hardcoded

The key is read from the environment at request time, never stored on the
instance and never logged. `availability()` reports whether one is *present*,
which is not the same as whether it *works* — so that answer is marked
`unverified`, because the only way to know is to spend a request.

## Uploading without loading the file

The multipart body is streamed from disk in chunks rather than assembled in
memory. A ten-minute chunk of 16 kHz mono is 19 MB; building the body as a
`bytes` would mean holding 19 MB per concurrent request, which at eight
workers is more memory than the transcription itself uses.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from .provider import Availability
from .types import (
    PermanentError,
    ProviderInfo,
    ProviderUnavailable,
    RetryableError,
    Segment,
    Transcript,
    Word,
)

__all__ = ["OpenAIConfig", "OpenAICompatibleProvider", "parse_verbose_json"]

#: OpenAI's documented ceiling. Larger uploads are rejected by the service, so
#: they are rejected here first with a message that says which knob to turn.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_READ_CHUNK = 1 << 16


@dataclass(slots=True)
class OpenAIConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "whisper-1"
    #: The *name* of the environment variable holding the key — not the key.
    #: Configuring which variable to read keeps a second provider on a second
    #: key possible without either being written down.
    api_key_env: str = "OPENAI_API_KEY"
    language: str = ""
    #: A short prompt biases spelling of names and jargon. Worth setting per
    #: channel: a motoring show gets "Koenigsegg" instead of "curious egg".
    prompt: str = ""
    temperature: float = 0.0
    timeout_s: float = 600.0
    organization: str = ""
    #: Extra multipart fields, for gateways with their own parameters.
    extra_fields: dict[str, str] = field(default_factory=dict)


class OpenAICompatibleProvider:
    """Any service speaking OpenAI's transcription protocol."""

    def __init__(
        self,
        config: OpenAIConfig | None = None,
        *,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.config = config or OpenAIConfig()
        self._opener = opener or urllib.request.build_opener()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="openai-compatible",
            model=self.config.model,
            remote=True,
            options={
                # The base URL is configuration worth recording — a transcript
                # from a self-hosted whisper.cpp is not the same artifact as
                # one from OpenAI. The key is not recorded, obviously.
                "base_url": self.config.base_url,
                "language": self.config.language,
                "temperature": self.config.temperature,
            },
        )

    # -- credentials -------------------------------------------------------

    def _api_key(self) -> str:
        """Read at request time. Never stored on the instance.

        A key on the object outlives the request in a heap dump, in a repr, in
        a pickled task payload. Reading it per request costs nothing and keeps
        it out of all three.

        An empty key is allowed through rather than refused: a local
        whisper.cpp server needs no authentication, and refusing here would
        make the most common self-hosted setup impossible.
        """

        return os.environ.get(self.config.api_key_env, "").strip()

    def availability(self) -> Availability:
        if not self.config.base_url:
            return Availability(False, "no base URL configured")
        key = self._api_key()
        if not key and "api.openai.com" in self.config.base_url:
            return Availability(
                False,
                f"{self.config.api_key_env} is not set, and api.openai.com "
                f"requires a key",
            )
        return Availability(
            True,
            f"configured for {self.config.base_url}"
            + (f" with a key from {self.config.api_key_env}" if key
               else " with no key (a local server)"),
            # Presence is not validity. The only way to know a key works is to
            # spend a request, and a health check that costs money is a health
            # check nobody runs.
            unverified=True,
        )

    # -- inference ---------------------------------------------------------

    def transcribe(self, wav_path: str, *, language: str = "") -> Transcript:
        if not os.path.exists(wav_path):
            raise PermanentError(f"no such audio: {wav_path}")
        size = os.path.getsize(wav_path)
        if size > MAX_UPLOAD_BYTES:
            raise PermanentError(
                f"{os.path.basename(wav_path)} is {size / 1e6:.1f} MB, over the "
                f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB limit — lower "
                f"AudioConfig.chunk_s so chunks come in under it"
            )

        fields: dict[str, str] = {
            "model": self.config.model,
            "response_format": "verbose_json",
            "temperature": str(self.config.temperature),
        }
        chosen = language or self.config.language
        if chosen:
            fields["language"] = chosen
        if self.config.prompt:
            fields["prompt"] = self.config.prompt
        fields.update(self.config.extra_fields)

        boundary = f"----clipforge{uuid.uuid4().hex}"
        # `timestamp_granularities[]` is repeated, not a single value, so it
        # cannot go in the dict above. Asking for `segment` as well as `word`
        # matters: requesting only `word` makes some servers omit segments
        # entirely, and the detector scores segments.
        body = _MultipartBody(boundary, fields, wav_path,
                              repeated=[("timestamp_granularities[]", "word"),
                                        ("timestamp_granularities[]", "segment")])

        url = self.config.base_url.rstrip("/") + "/audio/transcriptions"
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        request.add_header("Content-Length", str(len(body)))
        if key := self._api_key():
            request.add_header("Authorization", f"Bearer {key}")
        if self.config.organization:
            request.add_header("OpenAI-Organization", self.config.organization)

        try:
            with self._opener.open(request, timeout=self.config.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise _classify_status(error) from error
        except urllib.error.URLError as error:
            raise RetryableError(
                f"could not reach {url}: {getattr(error, 'reason', error)}"
            ) from error
        except TimeoutError as error:
            raise RetryableError(f"{url} timed out") from error
        except json.JSONDecodeError as error:
            raise RetryableError(
                f"{url} answered with something that is not JSON"
            ) from error

        return parse_verbose_json(payload, self.info)


# ---------------------------------------------------------------------------
# Multipart, streamed
# ---------------------------------------------------------------------------


class _MultipartBody(io.RawIOBase):
    """A multipart body that reads the file from disk as it is sent.

    `urllib` accepts any file-like object as `data` and will read it in
    chunks, so the audio never exists in memory as a whole. `__len__` computes
    the length arithmetically — the header sizes are known and the file's size
    comes from `stat` — which is what lets `Content-Length` be set without
    building the body first.
    """

    def __init__(
        self,
        boundary: str,
        fields: dict[str, str],
        file_path: str,
        repeated: list[tuple[str, str]] | None = None,
    ) -> None:
        self._path = file_path
        filename = os.path.basename(file_path)
        content_type = (
            mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )

        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(_field(boundary, name, value))
        for name, value in repeated or ():
            parts.append(_field(boundary, name, value))

        self._prefix = b"".join(parts) + (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        self._suffix = f"\r\n--{boundary}--\r\n".encode()
        self._file_size = os.path.getsize(file_path)

        self._handle: BinaryIO | None = None
        self._stage = 0          # 0 prefix, 1 file, 2 suffix, 3 done
        self._position = 0

    def __len__(self) -> int:
        return len(self._prefix) + self._file_size + len(self._suffix)

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return b"".join(iter(lambda: self.read(_READ_CHUNK), b""))
        while True:
            if self._stage == 0:
                chunk = self._prefix[self._position:self._position + size]
                self._position += len(chunk)
                if self._position >= len(self._prefix):
                    self._stage, self._position = 1, 0
                    self._handle = open(self._path, "rb")
                if chunk:
                    return chunk
                continue
            if self._stage == 1:
                assert self._handle is not None
                chunk = self._handle.read(size)
                if chunk:
                    return chunk
                self._handle.close()
                self._handle = None
                self._stage, self._position = 2, 0
                continue
            if self._stage == 2:
                chunk = self._suffix[self._position:self._position + size]
                self._position += len(chunk)
                if self._position >= len(self._suffix):
                    self._stage = 3
                if chunk:
                    return chunk
                continue
            return b""

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        super().close()


def _field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


def parse_verbose_json(payload: dict[str, Any], provider: ProviderInfo) -> Transcript:
    """OpenAI's `verbose_json` onto `Transcript`.

    A plain function, so the mapping can be tested against recorded payloads.
    Tolerant about which pieces are present: gateways implementing the same
    protocol vary in whether they return word timings, segments, or only text,
    and a missing part should degrade the transcript rather than fail it.
    """

    words = tuple(
        Word(
            text=str(item.get("word", "")).strip(),
            start_s=float(item.get("start", 0.0) or 0.0),
            end_s=float(item.get("end", 0.0) or 0.0),
            # OpenAI's word objects carry no confidence at all. `None` is the
            # true answer; a 1.0 here would be a fabrication that something
            # downstream eventually filters on.
            confidence=None,
        )
        for item in (payload.get("words") or [])
        if str(item.get("word", "")).strip()
    )

    segments: list[Segment] = []
    for item in payload.get("segments") or []:
        start = float(item.get("start", 0.0) or 0.0)
        end = float(item.get("end", 0.0) or 0.0)
        segments.append(Segment(
            text=str(item.get("text", "")).strip(),
            start_s=start,
            end_s=end,
            # Word objects come back in a flat top-level list rather than
            # nested per segment, so they are distributed by time here.
            words=tuple(w for w in words if start <= w.start_s < end) if words else (),
            avg_logprob=_optional_float(item.get("avg_logprob")),
            no_speech_prob=_optional_float(item.get("no_speech_prob")),
        ))

    return Transcript(
        text=str(payload.get("text", "")).strip(),
        segments=tuple(segments),
        words=words,
        language=_normalise_language(str(payload.get("language", "") or "")),
        duration_s=float(payload.get("duration", 0.0) or 0.0),
        provider=provider,
    )


#: OpenAI answers `language` with an English name ("english"), not a code.
#: Everything downstream expects ISO 639-1, so the common ones are mapped and
#: anything unrecognised is passed through rather than guessed at.
_LANGUAGE_NAMES = {
    "english": "en", "spanish": "es", "german": "de", "french": "fr",
    "portuguese": "pt", "italian": "it", "dutch": "nl", "polish": "pl",
    "russian": "ru", "japanese": "ja", "korean": "ko", "chinese": "zh",
    "arabic": "ar", "hindi": "hi", "turkish": "tr", "swedish": "sv",
    "norwegian": "no", "danish": "da", "finnish": "fi", "ukrainian": "uk",
}


def _normalise_language(value: str) -> str:
    lowered = value.strip().lower()
    if not lowered:
        return ""
    if len(lowered) == 2:
        return lowered
    return _LANGUAGE_NAMES.get(lowered, lowered)


def _classify_status(error: urllib.error.HTTPError) -> Exception:
    """What a caller should do about this status.

    401 and 403 are configuration and will fail identically for ever; 400 and
    413 are the request; 429 and 5xx are worth another pass. Getting this
    wrong in either direction is expensive — retrying a bad key burns the
    queue, and giving up on a rate limit drops the work.
    """

    status = error.code
    detail = _error_detail(error)
    if status in (401, 403):
        raise_as = ProviderUnavailable
        return raise_as(
            f"the transcription service rejected the credentials "
            f"(HTTP {status}): {detail}"
        )
    if status == 429:
        return RetryableError(f"rate limited (HTTP 429): {detail}")
    if status == 413:
        return PermanentError(f"the audio was too large for the service: {detail}")
    if 400 <= status < 500:
        return PermanentError(f"the service refused the request "
                              f"(HTTP {status}): {detail}")
    return RetryableError(f"the service failed (HTTP {status}): {detail}")


def _error_detail(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an error body is not required to be JSON
        return error.reason or ""
    if isinstance(payload, dict):
        inner = payload.get("error")
        if isinstance(inner, dict):
            return str(inner.get("message", "")) or str(inner)
        if inner:
            return str(inner)
    return str(payload)[:200]


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
