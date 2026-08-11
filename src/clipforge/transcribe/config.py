"""Choosing a provider from the environment.

    CLIPFORGE_TRANSCRIBE_PROVIDER=local_whisper
    CLIPFORGE_TRANSCRIBE_MODEL=small
    CLIPFORGE_TRANSCRIBE_DEVICE=cuda

    CLIPFORGE_TRANSCRIBE_PROVIDER=openai
    CLIPFORGE_TRANSCRIBE_BASE_URL=https://api.openai.com/v1
    CLIPFORGE_TRANSCRIBE_API_KEY_ENV=OPENAI_API_KEY   # the *name* of the var

Deployment picks the provider; no code changes and no redeploy of anything but
configuration. Pointing `BASE_URL` at a local `whisper.cpp` or
`faster-whisper-server` is how the OpenAI path runs with no third party
involved at all.

## Keys

`CLIPFORGE_TRANSCRIBE_API_KEY_ENV` holds the *name of the variable* that holds
the key, not the key. That indirection is worth the moment of confusion: it
keeps secrets out of this module, out of any config object that might be
logged or pickled, and out of the process's own configuration surface — and it
lets two providers read two different keys without either being written down
here.

There is no default key, no fallback key, and nothing in this package ever
writes one to a log or a database.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .audio import AudioConfig
from .openai_api import OpenAICompatibleProvider, OpenAIConfig
from .provider import Availability, TranscriptionProvider
from .sphinx import SphinxConfig, SphinxProvider
from .types import ProviderUnavailable
from .whisper_local import LocalWhisperConfig, LocalWhisperProvider

__all__ = [
    "PROVIDERS",
    "provider_from_env",
    "audio_config_from_env",
    "describe_environment",
    "ENV_PREFIX",
]

ENV_PREFIX = "CLIPFORGE_TRANSCRIBE_"

#: The names `CLIPFORGE_TRANSCRIBE_PROVIDER` accepts.
PROVIDERS = ("local_whisper", "openai", "pocketsphinx")

#: No default provider. An unset variable is an error rather than a silent
#: choice: every option here has a different cost, a different accuracy and a
#: different answer to "did my media leave the building?", and guessing on the
#: operator's behalf is guessing about all three.
_DEFAULT_PROVIDER = ""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"{ENV_PREFIX}{name}", default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _number(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def audio_config_from_env(base: AudioConfig | None = None) -> AudioConfig:
    """Audio extraction settings from the environment.

    Chunk length is worth exposing: an OpenAI-compatible gateway with a lower
    upload ceiling than OpenAI's needs shorter chunks, and finding that out
    means changing a variable rather than a constant.
    """

    config = base or AudioConfig()
    config.chunk_s = _number("CHUNK_S", config.chunk_s)
    config.overlap_s = _number("OVERLAP_S", config.overlap_s)
    config.chunk_threshold_s = _number("CHUNK_THRESHOLD_S", config.chunk_threshold_s)
    config.max_duration_s = _number("MAX_DURATION_S", config.max_duration_s)
    config.timeout_s = _number("EXTRACT_TIMEOUT_S", config.timeout_s)
    config.ffmpeg = os.environ.get("CLIPFORGE_FFMPEG", config.ffmpeg)
    return config


def provider_from_env(
    name: str = "", *, overrides: dict[str, Any] | None = None
) -> TranscriptionProvider:
    """Build the configured provider.

    Raises `ProviderUnavailable` for an unset or unknown name rather than
    falling back to something that happens to be installed. A pipeline that
    quietly picks a different transcriber than the operator configured is a
    pipeline producing captions from a model nobody chose.
    """

    chosen = (name or _env("PROVIDER", _DEFAULT_PROVIDER)).lower().replace("-", "_")
    if not chosen:
        raise ProviderUnavailable(
            f"no transcription provider configured — set "
            f"{ENV_PREFIX}PROVIDER to one of: {', '.join(PROVIDERS)}"
        )
    builder = _BUILDERS.get(chosen)
    if builder is None:
        raise ProviderUnavailable(
            f"unknown transcription provider {chosen!r} — expected one of: "
            f"{', '.join(PROVIDERS)}"
        )
    return builder(overrides or {})


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_local_whisper(overrides: dict[str, Any]) -> TranscriptionProvider:
    config = LocalWhisperConfig(
        model=_env("MODEL", "small"),
        device=_env("DEVICE", "cpu"),
        compute_type=_env("COMPUTE_TYPE", "int8"),
        beam_size=int(_number("BEAM_SIZE", 5)),
        vad_filter=_flag("VAD", True),
        language=_env("LANGUAGE"),
        download_root=_env("MODEL_ROOT"),
        cpu_threads=int(_number("CPU_THREADS", 0)),
    )
    for key, value in overrides.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return LocalWhisperProvider(config)


def _build_openai(overrides: dict[str, Any]) -> TranscriptionProvider:
    config = OpenAIConfig(
        base_url=_env("BASE_URL", "https://api.openai.com/v1"),
        model=_env("MODEL", "whisper-1"),
        # The name of the variable, never the value.
        api_key_env=_env("API_KEY_ENV", "OPENAI_API_KEY"),
        language=_env("LANGUAGE"),
        prompt=_env("PROMPT"),
        temperature=_number("TEMPERATURE", 0.0),
        timeout_s=_number("TIMEOUT_S", 600.0),
        organization=_env("ORGANIZATION"),
    )
    for key, value in overrides.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return OpenAICompatibleProvider(config)


def _build_sphinx(overrides: dict[str, Any]) -> TranscriptionProvider:
    config = SphinxConfig(
        model_path=_env("MODEL_ROOT"),
        language=_env("LANGUAGE", "en"),
    )
    for key, value in overrides.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return SphinxProvider(config)


_BUILDERS: dict[str, Callable[[dict[str, Any]], TranscriptionProvider]] = {
    "local_whisper": _build_local_whisper,
    "whisper": _build_local_whisper,
    "faster_whisper": _build_local_whisper,
    "openai": _build_openai,
    "openai_compatible": _build_openai,
    "pocketsphinx": _build_sphinx,
    "sphinx": _build_sphinx,
}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def describe_environment() -> dict[str, Any]:
    """What is configured and what can actually run.

    Written for an operator and for a startup log. It reports every provider's
    availability, not only the selected one, because "the configured provider
    cannot run" is far more useful next to "and here is what could".

    No secret is read or echoed: the key *variable name* appears, its value
    never does.
    """

    selected = _env("PROVIDER", _DEFAULT_PROVIDER)
    report: dict[str, Any] = {
        "selected": selected or None,
        "env_prefix": ENV_PREFIX,
        "api_key_env": _env("API_KEY_ENV", "OPENAI_API_KEY"),
        "api_key_present": bool(
            os.environ.get(_env("API_KEY_ENV", "OPENAI_API_KEY"), "").strip()
        ),
        "providers": {},
    }
    for name in PROVIDERS:
        try:
            availability = provider_from_env(name).availability()
        except ProviderUnavailable as error:
            availability = Availability(False, str(error))
        report["providers"][name] = availability.to_dict()

    if selected:
        chosen = report["providers"].get(selected.lower().replace("-", "_"))
        report["ready"] = bool(chosen and chosen["ready"])
        report["unverified"] = bool(chosen and chosen.get("unverified"))
    else:
        report["ready"] = False
        report["unverified"] = False
    return report
