#!/usr/bin/env python3
"""Video in, word timestamps out.

    python demo/run_transcribe_demo.py --check
    python demo/run_transcribe_demo.py --input talk.mp4 --out words.json
    python demo/run_transcribe_demo.py --synthesise --provider pocketsphinx

The full stage, end to end: media file -> ffmpeg extracts 16 kHz mono PCM ->
the configured provider transcribes it -> JSON with word-level timings on
stdout or in a file. Long inputs are chunked and stitched, and the scratch
audio is deleted whether or not the run succeeds.

The provider comes from the environment, never from a flag defaulting to
something convenient:

    CLIPFORGE_TRANSCRIBE_PROVIDER=local_whisper   # faster-whisper, on this box
    CLIPFORGE_TRANSCRIBE_PROVIDER=openai          # any OpenAI-compatible API
    CLIPFORGE_TRANSCRIBE_PROVIDER=pocketsphinx    # offline smoke test only

`--check` prints what is configured and what can actually run, and is the
right first command on a new machine — it distinguishes "a key is set" from
"a key works", and names providers that are configured but unverified.

No API key is read from a flag or written anywhere. The OpenAI-compatible
provider reads its key from the variable named by
CLIPFORGE_TRANSCRIBE_API_KEY_ENV (default OPENAI_API_KEY) at request time.

`--synthesise` builds a short spoken clip with espeak-ng and ffmpeg so the
demo runs with no media to hand. It is a robot voice: pocketsphinx will
mis-hear some of it, which is the honest result for that recogniser, not a
bug in this stage.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from clipforge.transcribe import (  # noqa: E402
    AudioConfig,
    ProviderUnavailable,
    Transcript,
    TranscriptionConfig,
    TranscriptionEngine,
    TranscriptionError,
    audio_config_from_env,
    describe_environment,
    provider_from_env,
    to_timed_words,
)
from clipforge.store import MemoryDatabase, SourceRecord, TenantRecord  # noqa: E402

TENANT = "ten_demo"
SPOKEN = (
    "The transcription stage turns a long recording into words with "
    "timestamps. Every word carries a start and an end."
)


def _ffmpeg() -> str:
    return os.environ.get("CLIPFORGE_FFMPEG") or shutil.which("ffmpeg") or ""


def _synthesise(directory: str, ffmpeg: str) -> str:
    """A real spoken clip: espeak-ng speaks, ffmpeg muxes it under video.

    Synthesised speech, not synthesised *transcript* — the recogniser still
    has to do its job on a real waveform.
    """

    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if not espeak:
        raise SystemExit(
            "  --synthesise needs espeak-ng. Install it, or pass --input.\n"
        )
    raw = os.path.join(directory, "speech.wav")
    subprocess.run([espeak, "-s", "130", "-w", raw, SPOKEN],
                   check=True, capture_output=True)
    media = os.path.join(directory, "talk.mp4")
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
         "-i", raw,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", media],
        check=True, capture_output=True,
    )
    return media


def _report(transcript: Transcript) -> dict[str, object]:
    """The demo's JSON: the transcript plus what the pipeline actually consumes.

    `caption_words` is the millisecond form the caption engine is handed, so
    the output shows both the provider's answer and the value that reaches the
    next stage — the place a rounding or unit mistake would otherwise hide.
    """

    payload = transcript.to_dict()
    payload["caption_words"] = [
        {"text": word.text, "start_ms": word.start_ms, "end_ms": word.end_ms}
        for word in to_timed_words(transcript)
    ]
    return payload


def _print_summary(transcript: Transcript, media: str) -> None:
    confidence = transcript.mean_confidence
    print(f"\n  input      {media}")
    print(f"  provider   {transcript.provider.name} / "
          f"{transcript.provider.model or 'unnamed model'}")
    print(f"  language   {transcript.language or 'undetected'}"
          + (f" ({transcript.language_confidence:.0%} confident)"
             if transcript.language_confidence else ""))
    print(f"  duration   {transcript.duration_s:.2f}s")
    print(f"  words      {len(transcript.words)}"
          + ("" if transcript.has_word_timings else "  (no word timings!)"))
    print(f"  segments   {len(transcript.segments)}")
    print("  confidence "
          + (f"{confidence:.2f} mean" if confidence is not None
             else "not reported by this provider"))
    print(f"\n  {transcript.text[:300]}"
          + ("…" if len(transcript.text) > 300 else "") + "\n")
    for word in transcript.words[:8]:
        shown = (f"{word.confidence:.2f}" if word.confidence is not None
                 else "   —")
        print(f"    {word.start_s:7.2f} → {word.end_s:7.2f}  {shown}  "
              f"{word.text}")
    if len(transcript.words) > 8:
        print(f"    … {len(transcript.words) - 8} more")


def _check() -> int:
    report = describe_environment()
    print(f"\n  selected   {report['selected'] or 'nothing configured'}")
    print(f"  key var    {report['api_key_env']} "
          f"({'set' if report['api_key_present'] else 'not set'})")
    print()
    for name, state in report["providers"].items():
        mark = "ready " if state["ready"] else "no    "
        if state["ready"] and state.get("unverified"):
            mark = "unver."
        print(f"  {mark} {name:<15} {state['detail']}")
    if not report["selected"]:
        print("\n  Set CLIPFORGE_TRANSCRIBE_PROVIDER to choose one.\n")
        return 1
    ready = bool(report.get("ready"))
    print("\n  " + ("ready to transcribe" if ready
                    else "the selected provider cannot run") + "\n")
    return 0 if ready else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="", help="video or audio file")
    parser.add_argument("--synthesise", action="store_true",
                        help="build a short spoken clip instead of --input")
    parser.add_argument("--provider", default="",
                        help="override CLIPFORGE_TRANSCRIBE_PROVIDER")
    parser.add_argument("--language", default="",
                        help="ISO code; omit to let the provider detect it")
    parser.add_argument("--out", default="",
                        help="write JSON here instead of stdout")
    parser.add_argument("--check", action="store_true",
                        help="report configured providers and stop")
    parser.add_argument("--queue", action="store_true",
                        help="go through the job queue and persist the run, "
                             "rather than transcribing inline")
    args = parser.parse_args()

    if args.check:
        return _check()
    if not args.input and not args.synthesise:
        print("\n  Nothing to transcribe. Pass --input FILE, or --synthesise "
              "to generate one. --check reports what is configured.\n")
        return 2

    ffmpeg = _ffmpeg()
    if not ffmpeg:
        print("\n  ffmpeg not found. Install it or set CLIPFORGE_FFMPEG.\n")
        return 1

    try:
        provider = provider_from_env(args.provider)
    except ProviderUnavailable as error:
        print(f"\n  {error}\n")
        return 1

    availability = provider.availability()
    if not availability.ready:
        print(f"\n  {provider.info.name} cannot run: {availability.detail}\n")
        return 1
    if availability.unverified:
        print(f"\n  note: {availability.detail}")

    workspace = tempfile.mkdtemp(prefix="clipforge-demo-")
    try:
        media = args.input or _synthesise(workspace, ffmpeg)
        if not os.path.exists(media):
            print(f"\n  no such file: {media}\n")
            return 1

        audio = audio_config_from_env(AudioConfig(ffmpeg=ffmpeg))
        database = MemoryDatabase()
        with database.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Demo"))
            uow.sources.save(SourceRecord(id="src_demo", tenant_id=TENANT,
                                          title=os.path.basename(media),
                                          fingerprint="demo"))
        engine = TranscriptionEngine(
            database, TENANT, provider,
            config=TranscriptionConfig(workspace=workspace, audio=audio),
        )

        print(f"  extracting audio and transcribing with "
              f"{provider.info.name}…")
        if args.queue:
            engine.enqueue("src_demo", media, language=args.language)
            run = engine.run(limit=1)[0]
            print(f"  job state  {run.state} after {run.attempts} attempt(s)")
            if run.last_error:
                print(f"  error      {run.last_error}\n")
                return 1
            transcript = engine.transcript_for("src_demo")
            assert transcript is not None  # SUCCEEDED implies a stored one
        else:
            transcript = engine.transcribe(media, language=args.language)

        _print_summary(transcript, media)
        payload = json.dumps(_report(transcript), indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            print(f"\n  wrote      {args.out}\n")
        else:
            print()
            print(payload)
        return 0
    except TranscriptionError as error:
        print(f"\n  transcription failed: {error}\n")
        return 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
