"""Ranking, deduplication, and final selection.

Candidate generation deliberately over-produces: the same strong moment
appears as a dozen overlapping windows at different lengths and offsets. This
module picks the best framing of each moment and throws the rest away, then
enforces spread so the output is not eight clips from the same five minutes.
"""

from __future__ import annotations

from typing import Sequence

from .types import Moment

# Two windows overlapping by more than this are the same moment cut differently.
DEFAULT_IOU_THRESHOLD = 0.35

# Wall-clock bucket used for the diversity constraint.
DEFAULT_BUCKET_MS = 10 * 60 * 1000
DEFAULT_PER_BUCKET = 2


def sort_by_virality(moments: Sequence[Moment]) -> list[Moment]:
    """Rank by virality, breaking ties toward the earlier moment.

    The tie-break is not arbitrary: earlier material in a source is more often
    the setup or the headline claim, and it is what a viewer of the full piece
    would recognise.
    """
    return sorted(moments, key=lambda m: (-m.scores.virality, m.start_ms))


def suppress_overlaps(
    moments: Sequence[Moment], iou_threshold: float = DEFAULT_IOU_THRESHOLD
) -> list[Moment]:
    """Greedy non-maximum suppression over the time axis.

    Walk the ranked list, keep each moment that does not substantially overlap
    something already kept. Because the list is sorted by score, the surviving
    framing of each moment is the best-scoring one.
    """
    kept: list[Moment] = []
    for moment in sort_by_virality(moments):
        if any(
            moment.candidate.overlap_ratio(existing.candidate) > iou_threshold
            for existing in kept
        ):
            continue
        kept.append(moment)
    return kept


def enforce_diversity(
    moments: Sequence[Moment],
    bucket_ms: int = DEFAULT_BUCKET_MS,
    per_bucket: int = DEFAULT_PER_BUCKET,
) -> list[Moment]:
    """Cap how many clips may come from the same region of the source.

    Without this, a single animated ten-minute stretch dominates the output and
    the rest of a two-hour source is never represented — which reads to the
    customer as the engine having missed most of their content.

    Input is assumed already ranked; order is preserved.
    """
    counts: dict[int, int] = {}
    kept: list[Moment] = []
    for moment in moments:
        bucket = moment.start_ms // bucket_ms
        if counts.get(bucket, 0) >= per_bucket:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        kept.append(moment)
    return kept


def select(
    moments: Sequence[Moment],
    limit: int,
    min_virality: int = 0,
    iou_threshold: float = DEFAULT_IOU_THRESHOLD,
    bucket_ms: int = DEFAULT_BUCKET_MS,
    per_bucket: int = DEFAULT_PER_BUCKET,
    relax_diversity_if_short: bool = True,
) -> tuple[list[Moment], list[Moment]]:
    """Full selection pipeline. Returns `(top, ranked)`.

    `ranked` is every deduplicated moment in score order — kept because the
    performance feedback loop needs the candidates that were *not* chosen as
    negative examples. `top` is the deliverable.
    """
    deduped = suppress_overlaps(moments, iou_threshold=iou_threshold)
    ranked = [m for m in deduped if m.scores.virality >= min_virality]

    top = enforce_diversity(ranked, bucket_ms=bucket_ms, per_bucket=per_bucket)[:limit]

    # A short source has few buckets, so the diversity cap can starve the
    # output. Backfill from the ranked list rather than returning two clips
    # when the customer asked for eight and eight good ones exist.
    if relax_diversity_if_short and len(top) < limit:
        chosen = {id(m) for m in top}
        for moment in ranked:
            if len(top) >= limit:
                break
            if id(moment) not in chosen:
                top.append(moment)
                chosen.add(id(moment))
        top = sort_by_virality(top)

    return top, ranked
