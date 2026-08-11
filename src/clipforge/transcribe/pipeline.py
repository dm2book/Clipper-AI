"""Connecting transcription to the pipeline that was written before it.

The factory asks a `Transcriber` for `list[TimedWord]` given a `Source`. This
module supplies one that is backed by the transcription engine, so the chain
closes:

    acquisition  ->  transcription  ->  clip intelligence  ->  rendering

`NullTranscriber` stays where it is and stays the default. It refuses rather
than inventing timings, and that is the right behaviour for a factory nobody
has configured a provider for — the alternative is captions that drift
visibly against the audio, which is worse than no captions. What changes is
that there is now something real to configure it with.

## Read before transcribe

`EngineTranscriber` looks for a stored transcript first. Transcription is the
most expensive stage in the pipeline and the only one whose output never
changes for a given input, so a source that has been transcribed once is never
transcribed again — on a paid provider that is a second invoice, and either
way it is a second transcript that disagrees at the margins with captions
somebody already reviewed.
"""

from __future__ import annotations

import os

from ..captions.types import TimedWord
from ..factory.sources import Source
from .engine import TranscriptionEngine
from .types import PermanentError, Transcript

__all__ = ["EngineTranscriber", "to_timed_words"]


def to_timed_words(
    transcript: Transcript, *, speaker: str = "SPEAKER_00"
) -> list[TimedWord]:
    """`Transcript` into what the caption engine consumes.

    Milliseconds, because that is `TimedWord`'s unit and rounding at the
    boundary once beats rounding in three places later.

    Words with no duration are dropped. A zero-length word is a caption the
    renderer draws for zero frames, and a karaoke fill that divides by its own
    duration would produce an infinity there.
    """

    words: list[TimedWord] = []
    for word in transcript.words:
        start_ms = int(round(word.start_s * 1000))
        end_ms = int(round(word.end_s * 1000))
        if end_ms <= start_ms or not word.text.strip():
            continue
        words.append(TimedWord(text=word.text.strip(), start_ms=start_ms,
                               end_ms=end_ms, speaker=speaker))
    return words


class EngineTranscriber:
    """A `Transcriber` backed by `TranscriptionEngine`.

    Synchronous: the factory's pipeline expects `transcribe` to return words,
    not a job id. For a channel running at any volume the right shape is to
    queue transcription during acquisition and let this find the stored
    result — which is what `media_for` and the stored-transcript check are
    for. The inline path exists because a single source acquired by hand
    should not require a worker to be running.
    """

    def __init__(
        self,
        engine: TranscriptionEngine,
        *,
        media_root: str = "",
        allow_inline: bool = True,
    ) -> None:
        self.engine = engine
        #: Where acquisition put the media. A `Source` carries a URL rather
        #: than a path, so something has to bridge the two.
        self.media_root = media_root
        #: When False, a source with no stored transcript raises rather than
        #: transcribing here and now. The right setting for a worker pool
        #: where transcription is its own queue.
        self.allow_inline = allow_inline

    def transcribe(self, source: Source) -> list[TimedWord]:
        stored = self.engine.transcript_for(source.source_id)
        if stored is not None:
            return to_timed_words(stored)

        if not self.allow_inline:
            raise PermanentError(
                f"{source.source_id} has no stored transcript and inline "
                f"transcription is disabled — queue it with "
                f"TranscriptionEngine.enqueue first"
            )

        media_path = self.media_for(source)
        if not media_path:
            raise PermanentError(
                f"{source.source_id} has no local media to transcribe — "
                f"acquisition must run before transcription"
            )
        # Stored, not just returned: the read above is only a saving if the
        # inline path writes something for it to find. Otherwise every factory
        # cycle re-transcribes the same source.
        transcript = self.engine.transcribe_source(
            source.source_id, media_path, language=source.language
        )
        return to_timed_words(transcript)

    def media_for(self, source: Source) -> str:
        """Where this source's media landed.

        Preference order: the acquisition run's recorded path, then a file
        under `media_root` named for the source. The recorded path is
        authoritative — acquisition writes it — and the fallback exists for
        media that arrived some other way.
        """

        with self.engine.database.unit_of_work(self.engine.tenant_id) as uow:
            runs = uow.acquisitions.for_source(source.source_id)
        for run in runs:
            if run.media_path and os.path.exists(run.media_path):
                return run.media_path

        if not self.media_root:
            return ""
        for name in (source.source_id, os.path.basename(source.url or "")):
            if not name:
                continue
            for suffix in (".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mkv"):
                candidate = os.path.join(self.media_root, f"{name}{suffix}")
                if os.path.exists(candidate):
                    return candidate
        return ""
