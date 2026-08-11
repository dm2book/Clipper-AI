"""Transcription: media in, words with timings out.

    from clipforge.transcribe import TranscriptionEngine, provider_from_env

    engine = TranscriptionEngine(db, "ten_acme", provider=provider_from_env())
    engine.enqueue(source_id, media_path)
    engine.run(limit=2)
    transcript = engine.transcript_for(source_id)

The provider is chosen by environment variable, so a deployment moves between
local inference and a hosted API without a code change:

    CLIPFORGE_TRANSCRIBE_PROVIDER=local_whisper|openai|pocketsphinx

`describe_environment()` reports what is configured and what can actually run,
including which providers are configured-but-unverified — the difference
between "a key is set" and "a key works".

## Verification status in this repository

* **Audio extraction, chunking, merging, persistence, states, retries and the
  pipeline integration** are exercised against real media by
  `tests/test_transcribe.py`.
* **`SphinxProvider`** runs for real: a bundled offline model, real decoding,
  real word timings. Its accuracy is well below Whisper and it is a smoke-test
  recogniser, never a default.
* **`LocalWhisperProvider`** is unverified here — the model host is blocked in
  the environment this was built in, so no transcript has been produced by it.
  Its mapping and error handling are tested against recorded result shapes.
* **`OpenAICompatibleProvider`** is tested against a real local HTTP server
  speaking the documented protocol. No request has reached OpenAI from here.

`availability()` reports `unverified=True` in exactly those cases, so a
deployment can tell without reading this.
"""

from __future__ import annotations

from .audio import (
    AudioChunk,
    AudioConfig,
    extract_audio,
    extracted_audio,
    plan_chunks,
    sweep_workspace,
    wav_duration_s,
)
from .config import (
    PROVIDERS,
    audio_config_from_env,
    describe_environment,
    provider_from_env,
)
from .engine import (
    TRANSCRIBE_JOB,
    TranscriptionConfig,
    TranscriptionEngine,
    transcript_from_dict,
    transcript_to_dict,
)
from .openai_api import OpenAICompatibleProvider, OpenAIConfig, parse_verbose_json
from .pipeline import EngineTranscriber, to_timed_words
from .provider import Availability, TranscriptionProvider, merge_chunks
from .sphinx import SphinxConfig, SphinxProvider
from .types import (
    AudioExtractionFailed,
    PermanentError,
    ProviderInfo,
    ProviderUnavailable,
    RetryableError,
    Segment,
    Transcript,
    TranscriptionError,
    TranscriptionState,
    Word,
)
from .whisper_local import LocalWhisperConfig, LocalWhisperProvider

__all__ = [
    "TranscriptionEngine",
    "TranscriptionConfig",
    "TRANSCRIBE_JOB",
    "provider_from_env",
    "audio_config_from_env",
    "describe_environment",
    "PROVIDERS",
    "TranscriptionProvider",
    "Availability",
    "merge_chunks",
    "LocalWhisperProvider",
    "LocalWhisperConfig",
    "OpenAICompatibleProvider",
    "OpenAIConfig",
    "parse_verbose_json",
    "SphinxProvider",
    "SphinxConfig",
    "EngineTranscriber",
    "to_timed_words",
    "AudioConfig",
    "AudioChunk",
    "extract_audio",
    "extracted_audio",
    "plan_chunks",
    "wav_duration_s",
    "sweep_workspace",
    "Word",
    "Segment",
    "Transcript",
    "ProviderInfo",
    "TranscriptionState",
    "transcript_to_dict",
    "transcript_from_dict",
    "TranscriptionError",
    "RetryableError",
    "PermanentError",
    "AudioExtractionFailed",
    "ProviderUnavailable",
]
