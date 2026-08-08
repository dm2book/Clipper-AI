"""The viral detection engine — orchestration.

    transcript
      → detect signals            (cheap, every utterance)
      → generate candidate windows (anchored on signal hits)
      → triage with a fast LLM pass (optional)
      → deep-judge the survivors    (optional)
      → score, dedupe, diversify
      → top clips

Each stage is independently testable and the LLM stages are optional, so the
engine runs end to end with no network access and degrades to heuristics if
the provider is unavailable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Sequence

from . import candidates as candidate_gen
from . import detectors, features, ranking, scoring
from .llm import MomentJudge, NullJudge
from .ranking import DEFAULT_BUCKET_MS, DEFAULT_IOU_THRESHOLD, DEFAULT_PER_BUCKET
from .taxonomy import WEIGHTS_VERSION
from .types import (
    Candidate,
    DetectionResult,
    LlmVerdict,
    Moment,
    Transcript,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ViralConfig:
    """Tuning surface for one detection run."""

    # How many clips to return.
    max_clips: int = 8

    # Clips below this virality score are never returned, even if it means
    # returning fewer than `max_clips`. Shipping filler is worse than shipping
    # less: a customer who sees three weak clips concludes the product does not
    # work, where three good ones reads as selective.
    min_virality: int = 35

    # Candidate generation ceiling. Guards against a pathological transcript
    # producing tens of thousands of windows.
    max_candidates: int = 240

    # How many candidates survive triage and get the expensive deep pass.
    deep_judge_limit: int = 30

    # Deduplication and spread.
    iou_threshold: float = DEFAULT_IOU_THRESHOLD
    diversity_bucket_ms: int = DEFAULT_BUCKET_MS
    clips_per_bucket: int = DEFAULT_PER_BUCKET

    # Judges. Both default to no-ops so the engine is usable — and testable —
    # with no credentials. `build_default_judges()` wires up the real cascade.
    triage_judge: MomentJudge = field(default_factory=NullJudge)
    deep_judge: MomentJudge = field(default_factory=NullJudge)


def build_default_judges(
    triage_model: str = "claude-opus-5",
    deep_model: str = "claude-opus-5",
) -> tuple[MomentJudge, MomentJudge]:
    """Construct the two-tier LLM cascade.

    Both tiers default to the same model, differing only in effort — triage
    runs at `low`, the deep pass at `high`. That alone is a large cost
    reduction, because effort drives the bulk of token spend.

    The architecture's cost model additionally assumes triage runs on a
    *cheaper* model. That is a spend decision rather than an engineering one,
    so it is not the default: pass `triage_model="claude-haiku-4-5"` to enable
    it, and measure the recall cost on your own material before committing.
    """
    from .llm import AnthropicJudge

    return (
        AnthropicJudge(model=triage_model, effort="low", max_tokens=8_000),
        AnthropicJudge(model=deep_model, effort="high", max_tokens=16_000),
    )


class ViralDetectionEngine:
    """Detects and ranks viral moments in a transcript."""

    def __init__(self, config: ViralConfig | None = None) -> None:
        self.config = config or ViralConfig()

    def detect(self, transcript: Transcript) -> DetectionResult:
        started = time.perf_counter()
        cfg = self.config

        if not transcript.utterances:
            return DetectionResult(
                source_id=transcript.source_id,
                top=[],
                ranked=[],
                stats={"reason": "empty transcript", "weights_version": WEIGHTS_VERSION},
            )

        # 1. Signals ---------------------------------------------------------
        hits = detectors.detect_all(transcript)

        # 2. Candidates ------------------------------------------------------
        pool = candidate_gen.generate(transcript, hits, max_candidates=cfg.max_candidates)
        used_fallback = False
        if not pool:
            # No detector fired anywhere. Rather than return nothing, sample
            # the source uniformly and let scoring judge it — a calm explainer
            # trips no keyword patterns but can still clip well.
            pool = candidate_gen.fallback_windows(transcript)
            used_fallback = True

        # 3. Triage ----------------------------------------------------------
        triaged = self._score_all(transcript, pool, verdicts={})
        shortlist = [
            m.candidate
            for m in ranking.sort_by_virality(triaged)[: cfg.deep_judge_limit]
        ]

        verdicts: dict[tuple[int, int], LlmVerdict] = {}
        if shortlist:
            verdicts.update(self._run_judge(cfg.triage_judge, shortlist, "triage"))

            # Re-rank on the triage verdicts before spending the deep pass, so
            # the expensive tier sees the shortlist the cheap tier actually
            # endorsed rather than the heuristic ordering.
            retriaged = self._score_all(transcript, shortlist, verdicts)
            deep_list = [
                m.candidate
                for m in ranking.sort_by_virality(retriaged)[: cfg.deep_judge_limit]
            ]
            verdicts.update(self._run_judge(cfg.deep_judge, deep_list, "deep"))

        # 4. Final scoring ---------------------------------------------------
        scored = self._score_all(transcript, pool, verdicts)

        # 5. Selection -------------------------------------------------------
        top, ranked = ranking.select(
            scored,
            limit=cfg.max_clips,
            min_virality=cfg.min_virality,
            iou_threshold=cfg.iou_threshold,
            bucket_ms=cfg.diversity_bucket_ms,
            per_bucket=cfg.clips_per_bucket,
        )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return DetectionResult(
            source_id=transcript.source_id,
            top=top,
            ranked=ranked,
            stats={
                "weights_version": WEIGHTS_VERSION,
                "duration_ms": transcript.duration_ms,
                "utterances": len(transcript.utterances),
                "signal_hits": len(hits),
                "candidates": len(pool),
                "llm_verdicts": len(verdicts),
                "deduped": len(ranked),
                "returned": len(top),
                "used_fallback_windows": used_fallback,
                "elapsed_ms": elapsed_ms,
            },
        )

    # -- internals -----------------------------------------------------------

    def _run_judge(
        self,
        judge: MomentJudge,
        batch: Sequence[Candidate],
        label: str,
    ) -> dict[tuple[int, int], LlmVerdict]:
        if isinstance(judge, NullJudge) or not batch:
            return {}
        try:
            results = judge.judge(batch)
        except Exception:
            # Never let a judge failure fail the source — heuristics alone
            # still produce a usable, if less precise, result.
            log.exception("%s judge failed; continuing without it", label)
            return {}
        return {v.candidate_span: v for v in results}

    def _score_all(
        self,
        transcript: Transcript,
        pool: Sequence[Candidate],
        verdicts: dict[tuple[int, int], LlmVerdict],
    ) -> list[Moment]:
        moments: list[Moment] = []
        for candidate in pool:
            verdict = verdicts.get(candidate.span)
            structural = features.extract(candidate, transcript)
            scores, signals, merged = scoring.score_candidate(
                candidate,
                structural,
                verdict=verdict,
                extra_signals=verdict.signals if verdict else (),
            )
            moments.append(
                Moment(
                    candidate=candidate,
                    scores=scores,
                    features=merged,
                    signals=signals,
                    title=verdict.title if verdict else _fallback_title(candidate),
                    rationale=verdict.rationale if verdict else _fallback_rationale(signals),
                    judged_by_llm=verdict is not None,
                )
            )
        return moments


def _fallback_title(candidate: Candidate) -> str:
    """A serviceable title when no LLM ran: the opening clause, trimmed."""
    text = " ".join(candidate.text.split())
    for stop in (". ", "! ", "? "):
        if stop in text[:90]:
            text = text[: text.index(stop) + 1]
            break
    return text[:70].rstrip(" ,;:-") or "Untitled moment"


def _fallback_rationale(signals: dict) -> str:
    if not signals:
        return "No categorical signals detected; selected on structure alone."
    ordered = sorted(signals.items(), key=lambda kv: -kv[1])[:3]
    parts = ", ".join(f"{s.value} ({v:.2f})" for s, v in ordered)
    return f"Detected: {parts}."
