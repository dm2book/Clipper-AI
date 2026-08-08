"""Tier two of the cascade — semantic judgement by an LLM.

The heuristic detectors are good at locating moments and bad at judging them.
They cannot tell whether a clip makes sense to someone who did not hear the
preceding hour, whether a joke lands, or whether a line is quotable. That is
what this tier is for.

The engine runs fine without it (`NullJudge`), which keeps the whole pipeline
testable offline and gives the product a degraded-but-working mode when the
LLM provider is down.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from .types import Candidate, LlmVerdict, Signal, clamp

log = logging.getLogger(__name__)

# Scored in one request per batch rather than one per candidate. The dominant
# cost is the shared rubric in the system prompt, so batching amortises it —
# and prompt caching makes the second batch onward nearly free.
DEFAULT_BATCH_SIZE = 25


class MomentJudge(Protocol):
    """Anything that can turn candidates into semantic verdicts."""

    def judge(self, candidates: Sequence[Candidate]) -> list[LlmVerdict]:
        ...


class NullJudge:
    """No-op judge. The engine falls back to pure heuristics."""

    def judge(self, candidates: Sequence[Candidate]) -> list[LlmVerdict]:
        return []


RUBRIC = """\
You are scoring candidate clips cut from a long-form transcript for use as \
short-form vertical video (TikTok, YouTube Shorts, Instagram Reels).

Score each candidate on four dimensions, each from 0.0 to 1.0:

hook_strength — Does the opening line earn the next two seconds? Viewers decide \
almost immediately. A concrete number, a stated stakes, a question, or a claim \
that contradicts expectation scores high. Throat-clearing ("so, yeah, I mean") \
scores near zero regardless of what follows.

standalone — Would someone who has never heard this source understand it with \
no setup? This is the single most common reason a clip fails. Unexplained \
pronouns, references to things said earlier, and jokes whose premise is missing \
score low. Be harsh here; most candidates deserve below 0.5.

payoff — Does the moment resolve inside the clip, or does it get cut off \
mid-thought? A story that reaches its point scores high. A setup with no \
punchline scores low even if the setup is excellent.

quotability — Is there a line someone would repeat, screenshot, or caption? \
Not whether the clip is good overall — specifically whether it contains a \
sentence that travels on its own.

Also identify which of these categories genuinely apply. Only list a category \
if it is clearly present; do not pad the list:
controversy, emotional_spike, money, funny, argument, debate, failure, \
success, secret, lesson

Finally, write a title (under 60 characters, no clickbait punctuation, written \
as the clip's own claim rather than a description of it) and a one-sentence \
rationale explaining the scores.

Judge only what is in each candidate's text. Do not assume favourable context \
that is not shown."""


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "hook_strength": {"type": "number"},
                    "standalone": {"type": "number"},
                    "payoff": {"type": "number"},
                    "quotability": {"type": "number"},
                    "signals": {
                        "type": "array",
                        "items": {"type": "string", "enum": [s.value for s in Signal]},
                    },
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "id",
                    "hook_strength",
                    "standalone",
                    "payoff",
                    "quotability",
                    "signals",
                    "title",
                    "rationale",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class AnthropicJudge:
    """Judge backed by the Claude API.

    Two tiers are supported via `effort`. The architecture calls for a cheap
    triage pass over every candidate followed by a deep pass over the top N;
    `ViralConfig` wires that up, and both tiers use this class with different
    effort levels.

    Note on `max_tokens`: thinking is on by default for Opus-tier models and
    counts against the same budget as the response, so the ceiling here has to
    cover both. Too tight and verdicts truncate mid-array.
    """

    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 16_000
    batch_size: int = DEFAULT_BATCH_SIZE
    use_refusal_fallback: bool = True
    client: Any = None

    def __post_init__(self) -> None:
        if self.client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "The `anthropic` package is required for AnthropicJudge. "
                    "Install it, or run the engine with NullJudge for "
                    "heuristics-only detection."
                ) from exc
            self.client = anthropic.Anthropic()

    def judge(self, candidates: Sequence[Candidate]) -> list[LlmVerdict]:
        verdicts: list[LlmVerdict] = []
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start : start + self.batch_size]
            try:
                verdicts.extend(self._judge_batch(batch))
            except Exception:
                # A failed batch degrades that batch to heuristics-only rather
                # than failing the whole source. Detection is best-effort by
                # design; losing every clip because one API call failed would
                # be a far worse outcome than losing some semantic precision.
                log.exception(
                    "LLM judging failed for candidates %s-%s; falling back to "
                    "heuristics for this batch",
                    start,
                    start + len(batch),
                )
        return verdicts

    def _judge_batch(self, batch: Sequence[Candidate]) -> list[LlmVerdict]:
        payload = [
            {
                "id": i,
                "duration_s": round(c.duration_s, 1),
                "text": c.text,
            }
            for i, c in enumerate(batch)
        ]

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": RUBRIC,
                    # The rubric is byte-identical across every batch and every
                    # source, so it caches once and is read thereafter.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "output_config": {
                "format": {"type": "json_schema", "schema": _SCHEMA},
                "effort": self.effort,
            },
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Score these candidate clips. Return one verdict per "
                        "candidate, preserving the `id` field.\n\n"
                        + json.dumps(payload, ensure_ascii=False, indent=1)
                    ),
                }
            ],
        }

        response = self._create(request)

        # Safety classifiers can decline a request and return HTTP 200 with an
        # empty or partial content array. Reading content[0] unconditionally
        # would raise here, so check the stop reason first.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            log.warning(
                "LLM declined to score a batch (category=%s); using heuristics "
                "for these candidates",
                getattr(details, "category", None),
            )
            return []

        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        if not text:
            return []

        return self._parse(text, batch)

    def _create(self, request: dict[str, Any]) -> Any:
        """Issue the request, preferring server-side refusal fallback.

        Fallback is opt-in and only exists on the beta endpoint, so we try it
        first and fall back to the stable endpoint if this SDK version or
        provider does not accept the parameter.
        """
        if self.use_refusal_fallback:
            try:
                return self.client.beta.messages.create(
                    **request,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except TypeError:
                log.debug("SDK does not accept `fallbacks`; using stable endpoint")
            except Exception as exc:  # pragma: no cover - provider dependent
                # Bedrock, Vertex and Foundry reject the parameter outright.
                if "fallback" not in str(exc).lower():
                    raise
                log.debug("Provider rejected `fallbacks`; using stable endpoint")

        return self.client.messages.create(**request)

    @staticmethod
    def _parse(text: str, batch: Sequence[Candidate]) -> list[LlmVerdict]:
        data = json.loads(text)
        verdicts: list[LlmVerdict] = []

        for row in data.get("verdicts", []):
            index = row.get("id")
            if not isinstance(index, int) or not 0 <= index < len(batch):
                log.warning("LLM returned an out-of-range candidate id: %r", index)
                continue

            signals: list[Signal] = []
            for name in row.get("signals", ()):
                try:
                    signals.append(Signal(name))
                except ValueError:
                    log.debug("LLM returned unknown signal %r; ignoring", name)

            verdicts.append(
                LlmVerdict(
                    candidate_span=batch[index].span,
                    # The schema cannot express numeric bounds, so clamp here.
                    hook_strength=clamp(float(row["hook_strength"])),
                    standalone=clamp(float(row["standalone"])),
                    payoff=clamp(float(row["payoff"])),
                    quotability=clamp(float(row["quotability"])),
                    title=str(row.get("title", "")).strip()[:120],
                    rationale=str(row.get("rationale", "")).strip(),
                    signals=tuple(signals),
                )
            )

        return verdicts
