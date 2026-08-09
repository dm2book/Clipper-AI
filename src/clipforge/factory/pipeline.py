"""The per-item pipeline: source in, scheduled post out.

    source → clear rights → transcribe → detect clips → rank hooks
           → build captions → compose the frame → schedule

Every stage is checkpointed onto the `WorkItem`, so a run that dies at stage
six resumes at stage six rather than paying for the first five again.
Transcription and detection are the expensive stages; repeating them because
a caption failed is how a factory's unit economics stop working.

**Stages fail closed.** An item that cannot clear rights, cannot reach the
quality floor, or cannot be afforded does not become a lesser post — it stops,
with the reason recorded. The alternative is a channel that publishes filler
whenever the good material runs out, which costs more audience than posting
nothing.

Two stages need the outside world and are therefore protocols with offline
defaults: `Transcriber` (speech to word timings) and the media renderer. Every
other stage runs on the engines in this repository, so the whole pipeline is
exercisable without network, credentials, or a video toolchain.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

from ..captions import generate as generate_captions
from ..captions.types import TimedWord
from ..gameplay import GameplayAsset, GameplayConfig, GameplayEngine, SpeakerTrack
from ..hooks import ClipContext, HookConfig, HookGenerator
from ..hooks.types import Hook
from ..publish.types import MediaAsset, Platform, PostSpec, Visibility, utcnow
from ..viral import Transcript, Utterance, ViralDetectionEngine
from ..viral.types import Moment
from .channel import Channel
from .niches import (
    domain_affinity,
    hook_preference,
    target_duration,
    uses_stream_clipper,
)
from .sources import Clearance, Source, clear


class Stage(str, enum.Enum):
    """Where an item has reached. Ordered."""

    DISCOVERED = "discovered"
    CLEARED = "cleared"
    TRANSCRIBED = "transcribed"
    CLIPPED = "clipped"
    HOOKED = "hooked"
    CAPTIONED = "captioned"
    COMPOSED = "composed"
    SCHEDULED = "scheduled"
    #: Stopped on purpose — rights, quality, budget, duplication.
    BLOCKED = "blocked"
    #: Stopped by an error.
    FAILED = "failed"


STAGE_ORDER: tuple[Stage, ...] = (
    Stage.DISCOVERED, Stage.CLEARED, Stage.TRANSCRIBED, Stage.CLIPPED,
    Stage.HOOKED, Stage.CAPTIONED, Stage.COMPOSED, Stage.SCHEDULED,
)

#: Rough per-stage cost in cents, used to refuse work that cannot be finished.
#: Transcription dominates, which is why sources arriving with one are
#: preferred by the finder.
STAGE_COST_CENTS: dict[Stage, int] = {
    Stage.CLEARED: 0,
    Stage.TRANSCRIBED: 120,
    Stage.CLIPPED: 15,
    Stage.HOOKED: 8,
    Stage.CAPTIONED: 2,
    Stage.COMPOSED: 45,      # render
    Stage.SCHEDULED: 1,
}
ITEM_COST_CENTS = sum(STAGE_COST_CENTS.values())


class Transcriber(Protocol):
    """Speech to word-level timings.

    Word level, not sentence level: the caption engine refuses sentence
    subtitles outright, because there is nothing to karaoke and nowhere precise
    to place an emoji.
    """

    def transcribe(self, source: Source) -> list[TimedWord]: ...


class NullTranscriber:
    """Refuses rather than inventing timings.

    The honest default. Faking word timings produces captions that drift
    visibly against the audio, and the drift is worse than having no captions
    at all — so an item with no transcript stops here and says so.
    """

    def transcribe(self, source: Source) -> list[TimedWord]:
        raise NotImplementedError(
            f"no transcriber configured and {source.source_id} has no "
            f"transcript — supply one, or wire a Transcriber"
        )


@dataclass(slots=True)
class WorkItem:
    """One source travelling through the pipeline, with its artifacts."""

    item_id: str
    channel_id: str
    source: Source
    stage: Stage = Stage.DISCOVERED
    reason: str = ""

    clearance: Clearance | None = None
    words: list[TimedWord] = field(default_factory=list)
    moment: Moment | None = None
    hooks: list[Hook] = field(default_factory=list)
    caption_track: Any = None
    gameplay_plan: Any = None
    post_specs: list[PostSpec] = field(default_factory=list)
    scheduled_post_ids: list[str] = field(default_factory=list)

    cost_cents: int = 0
    created_at: datetime = field(default_factory=utcnow)
    history: list[tuple[str, str]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.stage in (Stage.BLOCKED, Stage.FAILED)

    @property
    def best_hook(self) -> Hook | None:
        return self.hooks[0] if self.hooks else None

    def advance(self, stage: Stage, note: str = "") -> None:
        self.stage = stage
        self.cost_cents += STAGE_COST_CENTS.get(stage, 0)
        self.history.append((stage.value, note))

    def stop(self, stage: Stage, reason: str) -> None:
        self.stage = stage
        self.reason = reason
        self.history.append((stage.value, reason))

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "channel_id": self.channel_id,
            "source_id": self.source.source_id,
            "stage": self.stage.value,
            "reason": self.reason,
            "cost_cents": self.cost_cents,
            "virality": (
                round(self.moment.scores.virality, 1) if self.moment else None
            ),
            "hook": self.best_hook.text if self.best_hook else "",
            "predicted_ctr": (
                self.best_hook.estimate.percent if self.best_hook else ""
            ),
            "cues": len(self.caption_track.cues) if self.caption_track else 0,
            "scheduled": list(self.scheduled_post_ids),
            "history": [{"stage": s, "note": n} for s, n in self.history],
        }


@dataclass(slots=True)
class PipelineConfig:
    transcriber: Transcriber = field(default_factory=NullTranscriber)
    #: Gameplay beds available to composite behind talking-head niches.
    gameplay_library: tuple[GameplayAsset, ...] = ()
    #: Hooks generated per clip before niche re-ranking.
    hook_count: int = 20
    #: Base CTR the hook estimate is projected onto, for legibility only.
    baseline_ctr: float = 5.0
    #: Where rendered files are published from. Instagram needs a public URL.
    cdn_base: str = "https://cdn.clipforge.test"


class Pipeline:
    """Runs one item from source to scheduled post."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        self._viral = ViralDetectionEngine()

    def run(
        self,
        channel: Channel,
        source: Source,
        transcript_words: Sequence[TimedWord] = (),
        now: datetime | None = None,
    ) -> WorkItem:
        """Drive one source through every stage.

        Never raises. A pipeline that throws takes its channel down with it,
        and the whole point of per-channel isolation is that it should not.
        """
        now = now or utcnow()
        item = WorkItem(
            item_id=f"item_{uuid.uuid4().hex[:12]}",
            channel_id=channel.channel_id,
            source=source,
        )

        try:
            for stage in (
                self._clear, self._transcribe, self._detect, self._hook,
                self._caption, self._compose, self._specs,
            ):
                stage(channel, item, transcript_words, now)
                if item.blocked:
                    return item
        except NotImplementedError as error:
            item.stop(Stage.BLOCKED, str(error))
        except Exception as error:                      # noqa: BLE001
            item.stop(Stage.FAILED, f"{type(error).__name__}: {error}")

        return item

    # -- stages ----------------------------------------------------------------

    def _clear(self, channel: Channel, item: WorkItem, _words, now) -> None:
        if source_seen := (item.source.fingerprint in channel.used_fingerprints):
            item.stop(
                Stage.BLOCKED,
                f"already used by this channel — the same source republished "
                f"is noticed by an audience long before it is noticed by an "
                f"operator",
            )
            return
        del source_seen

        clearance = clear(
            item.source, channel.accepted_rights,
            monetised=channel.monetised, now=now,
        )
        item.clearance = clearance
        if not clearance.cleared:
            item.stop(Stage.BLOCKED, f"rights: {clearance.reason}")
            return

        if not channel.budget.can_afford(ITEM_COST_CENTS):
            item.stop(
                Stage.BLOCKED,
                f"budget: {channel.budget.remaining_cents}c left, item needs "
                f"~{ITEM_COST_CENTS}c. Starting work that cannot be finished "
                f"wastes the stages that do run.",
            )
            return

        item.advance(Stage.CLEARED, clearance.basis.value)

    def _transcribe(self, channel: Channel, item: WorkItem, words, now) -> None:
        if words:
            item.words = list(words)
        else:
            item.words = self.config.transcriber.transcribe(item.source)

        if not item.words:
            item.stop(Stage.BLOCKED, "transcription produced no words")
            return
        item.advance(Stage.TRANSCRIBED, f"{len(item.words)} words")

    def _detect(self, channel: Channel, item: WorkItem, _words, now) -> None:
        if uses_stream_clipper(channel.niche):
            item.stop(
                Stage.BLOCKED,
                f"{channel.niche.value} sources livestreams, which route to "
                f"the stream clipper (chat spikes) rather than the viral "
                f"engine (transcripts). Supply a StreamSession instead.",
            )
            return

        transcript = _to_transcript(item)
        result = self._viral.detect(transcript)

        # `ranked` rather than `top`: the engine's own shortlist is chosen on
        # general virality, and a niche's best moment can sit just outside it.
        pool = result.ranked or result.top
        source_note = ""

        if not pool:
            # The general detectors found nothing — which for Cars, Luxury and
            # History usually means the vocabulary is wrong rather than the
            # material is. Fall back to plain windows scored on the niche's own
            # terms, and record that it happened: a clip chosen this way had no
            # general signal behind it and deserves to be visible as such.
            fallback = self._domain_fallback(channel, transcript)
            if not fallback:
                item.stop(
                    Stage.BLOCKED,
                    "no moment cleared the detector, and no window matched "
                    f"{channel.niche.value} vocabulary either",
                )
                return
            pool = fallback
            source_note = " via niche vocabulary (no general signal)"

        scored = [
            (self._niche_score(channel, moment), moment) for moment in pool
        ]
        adjusted, best = max(scored, key=lambda pair: pair[0])

        if adjusted < channel.quality_floor:
            item.stop(
                Stage.BLOCKED,
                f"best moment scored {adjusted:.0f} for this niche, below "
                f"{channel.name}'s floor of {channel.quality_floor:.0f} — "
                f"publishing filler costs more audience than posting nothing",
            )
            return

        item.moment = best
        item.advance(
            Stage.CLIPPED,
            f"{adjusted:.0f} niche-adjusted "
            f"(base {best.scores.virality:.0f}), "
            f"{best.candidate.duration_ms / 1000:.0f}s{source_note}",
        )

    def _domain_fallback(
        self, channel: Channel, transcript: Transcript
    ) -> list[Moment]:
        """Windows anchored on niche vocabulary, when the general detector is mute.

        Anchored, not strided. A fixed-stride sweep will happily return a
        window whose domain words are all in its last sentence, and everything
        downstream then works on mostly filler — the hook writes itself about
        "a longer conversation" because that is what the text it was handed is
        about. Growing the window outward from the utterance that actually
        carries the vocabulary is the same trick the viral engine uses for
        signal hits, and it costs nothing.

        Deliberately strict. This path exists because a Cars clip can be
        excellent and score zero on detectors built around funding rounds — not
        so that every channel can publish whatever it likes when detection
        fails. Anything chosen here is marked in the item's history.
        """
        from ..viral.types import Candidate, Scores

        utterances = transcript.utterances
        if not utterances:
            return []

        low, high = channel.profile.duration_s
        target_ms = int(target_duration(channel.niche) * 1000)
        out: list[Moment] = []
        seen: set[tuple[int, int]] = set()

        for anchor, utterance in enumerate(utterances):
            if domain_affinity(channel.niche, utterance.text) <= 0.0:
                continue

            # Grow outward from the anchor, preferring to extend backwards
            # only as far as needed — a hook reads the opening words.
            first = last = anchor
            while True:
                span = utterances[last].end_ms - utterances[first].start_ms
                if span >= target_ms:
                    break
                extended = False
                if last + 1 < len(utterances):
                    last += 1
                    extended = True
                elif first > 0:
                    first -= 1
                    extended = True
                if not extended:
                    break

            if (first, last) in seen:
                continue
            seen.add((first, last))

            window = utterances[first:last + 1]
            duration = (window[-1].end_ms - window[0].start_ms) / 1000.0
            if not (low * 0.7 <= duration <= high * 1.3):
                continue

            text = " ".join(u.text for u in window)
            affinity = domain_affinity(channel.niche, text)
            if affinity < 0.5:
                continue

            candidate = Candidate(
                first_utterance=window[0].index,
                last_utterance=window[-1].index,
                start_ms=window[0].start_ms,
                end_ms=window[-1].end_ms,
                text=text,
                hits=(),
            )

            # A synthetic score in the same range as the engine's, so the
            # channel's quality floor still means what it means everywhere
            # else. Capped below what a signal-backed moment can reach.
            base = 40.0 + 30.0 * affinity
            out.append(Moment(
                candidate=candidate,
                scores=Scores(
                    virality=base, engagement=base, retention=base,
                    comment=base * 0.8, share=base * 0.9,
                ),
                features={"domain_affinity": affinity},
                signals=(f"{channel.niche.value}_vocabulary",),
                title=text[:60],
                rationale=(
                    f"no general signal; anchored on {channel.niche.value} "
                    f"vocabulary at affinity {affinity:.2f}"
                ),
            ))

        return out

    @staticmethod
    def _niche_score(channel: Channel, moment: Moment) -> float:
        """The general virality score, corrected for what this channel is about.

        Two corrections, both bounded so the base score still dominates:

        The **domain bonus** exists because the viral engine's detectors were
        tuned on founder and podcast material. A Cars or History moment can be
        the best thirty seconds in an hour and register no signal hits at all,
        which reads as "nothing here" rather than "wrong vocabulary".

        The **duration fit** keeps a niche inside its own shape. A 50-second
        moment is a good History clip and a bad Gaming one, and the general
        engine has no way to know which channel is asking.
        """
        profile = channel.profile
        base = moment.scores.virality

        affinity = domain_affinity(channel.niche, moment.candidate.text)
        low, high = profile.duration_s
        duration = moment.candidate.duration_ms / 1000.0
        if low <= duration <= high:
            fit = 1.0
        else:
            miss = low - duration if duration < low else duration - high
            fit = max(0.6, 1.0 - miss / max(1.0, high))

        return base * (1.0 + 0.35 * affinity) * fit

    def _hook(self, channel: Channel, item: WorkItem, _words, now) -> None:
        moment = item.moment
        assert moment is not None

        signals = tuple(
            dict.fromkeys(tuple(moment.signals) + channel.profile.signals)
        )
        generator = HookGenerator(HookConfig(
            count=self.config.hook_count,
            baseline_ctr=self.config.baseline_ctr,
        ))
        result = generator.generate(ClipContext(
            text=moment.candidate.text,
            signals=signals,
            duration_s=moment.candidate.duration_ms / 1000.0,
            language=channel.profile.language,
        ))

        if not result.hooks:
            item.stop(Stage.BLOCKED, "no hook could be generated")
            return

        # Re-rank for the niche. A thumb on the scale, not a filter: the hook
        # engine's own features still decide, so a strong off-type hook wins.
        ranked = sorted(
            result.hooks,
            key=lambda hook: -(
                hook.estimate.lift * hook_preference(channel.niche, hook.hook_type)
            ),
        )
        item.hooks = ranked
        item.advance(
            Stage.HOOKED,
            f"{len(ranked)} hooks, best {ranked[0].hook_type.value}",
        )

    def _caption(self, channel: Channel, item: WorkItem, _words, now) -> None:
        moment = item.moment
        assert moment is not None

        start, end = moment.candidate.start_ms, moment.candidate.end_ms
        window = [
            TimedWord(
                text=word.text,
                start_ms=word.start_ms - start,
                end_ms=word.end_ms - start,
                speaker=word.speaker,
            )
            for word in item.words
            if word.start_ms >= start and word.end_ms <= end
        ]

        if not window:
            item.stop(Stage.BLOCKED, "no words fall inside the chosen clip")
            return

        item.caption_track = generate_captions(
            window,
            style=channel.profile.caption_style,
            language=channel.profile.language,
        )
        item.advance(
            Stage.CAPTIONED,
            f"{len(item.caption_track.cues)} cues, "
            f"{channel.profile.caption_style}",
        )

    def _compose(self, channel: Channel, item: WorkItem, _words, now) -> None:
        bed = channel.profile.gameplay_bed
        moment = item.moment
        assert moment is not None
        duration = moment.candidate.duration_ms / 1000.0

        assets = (
            tuple(a for a in self.config.gameplay_library if a.game is bed)
            if bed else ()
        )
        if bed and not assets:
            item.stop(
                Stage.BLOCKED,
                f"{channel.niche.value} wants a {bed.value} bed and the "
                f"library has none",
            )
            return

        engine = GameplayEngine(GameplayConfig(
            game=bed, seed=item.item_id,
        ))
        item.gameplay_plan = engine.compose(
            duration_s=duration,
            track=SpeakerTrack(),          # no face track in this path
            assets=assets,
            word_count=len(moment.candidate.text.split()),
        )
        item.advance(
            Stage.COMPOSED,
            f"{item.gameplay_plan.style.value}"
            + (f" over {bed.value}" if bed else " (no bed — footage is the visual)"),
        )

    def _specs(self, channel: Channel, item: WorkItem, _words, now) -> None:
        moment = item.moment
        hook = item.best_hook
        assert moment is not None and hook is not None

        duration = moment.candidate.duration_ms / 1000.0
        asset = MediaAsset(
            asset_id=item.item_id,
            path=f"/renders/{item.item_id}.mp4",
            public_url=f"{self.config.cdn_base}/{item.item_id}.mp4",
            size_bytes=int(duration * 0.7 * 1024**2),
            duration_s=duration,
            width=1080, height=1920, fps=60,
        )

        caption = hook.text
        attribution = (
            item.clearance.required_attribution if item.clearance else ""
        )
        if attribution:
            # Attribution is a licence condition, not a nicety. Dropping it
            # voids the licence the clip is published under.
            caption = f"{caption}\n\n{attribution}"

        for platform in channel.platforms:
            item.post_specs.append(PostSpec(
                asset=asset,
                title=hook.text[:100],
                caption=caption,
                visibility=Visibility.PUBLIC,
                metadata={
                    "channel_id": channel.channel_id,
                    "niche": channel.niche.value,
                    "source_id": item.source.source_id,
                    "rights_basis": (
                        item.clearance.basis.value if item.clearance else ""
                    ),
                    "hook_type": hook.hook_type.value,
                    "predicted_lift": hook.estimate.lift,
                    "virality": moment.scores.virality,
                },
            ))

        if not item.post_specs:
            item.stop(Stage.BLOCKED, "no connected accounts to post to")
            return

        item.advance(
            Stage.SCHEDULED, f"{len(item.post_specs)} platform(s)"
        )


#: A pause longer than this is a sentence boundary.
UTTERANCE_GAP_MS = 700
_SENTENCE_END = (".", "?", "!", "…")


def _to_transcript(item: WorkItem) -> Transcript:
    """Group word timings into utterances for the viral engine.

    Splits on **all three** of speaker change, pause, and sentence-ending
    punctuation. Using only pauses looks sufficient and is not: ASR that emits
    tight word timings produces no gap above the threshold, the whole clip
    collapses into one utterance, and the viral engine — which anchors
    candidate windows on utterance boundaries — is left with a single candidate
    spanning everything. Detection then either returns that one window or
    nothing, which is indistinguishable from the detector being broken.
    """
    utterances: list[Utterance] = []
    current: list[TimedWord] = []
    speaker = item.words[0].speaker if item.words else ""

    def flush() -> None:
        if not current:
            return
        utterances.append(Utterance(
            index=len(utterances),
            start_ms=current[0].start_ms,
            end_ms=current[-1].end_ms,
            speaker=speaker,
            text=" ".join(w.text for w in current),
            words=tuple(w.text for w in current),
        ))

    for word in item.words:
        if current and (
            word.speaker != speaker
            or word.start_ms - current[-1].end_ms > UTTERANCE_GAP_MS
        ):
            flush()
            current = []
            speaker = word.speaker

        current.append(word)

        if word.text.rstrip('"\'”’').endswith(_SENTENCE_END):
            flush()
            current = []
    flush()

    return Transcript(
        source_id=item.source.source_id,
        utterances=tuple(utterances),
        language=item.source.language,
    )
