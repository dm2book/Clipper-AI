"""Optional LLM hook writing.

Templates guarantee twenty hooks offline and are genuinely decent, but they
cannot reference the specific thing that makes a clip funny, and they reuse
phrasing across a creator's whole library — which viewers notice before the
creator does.

The LLM tier writes hooks that name what actually happened. It is off by
default: the engine returns a full set without it, so the test suite runs
offline and the product degrades rather than fails when the provider is down.
LLM hooks are scored by the same estimator as template hooks, so the two
compete on equal terms rather than the model's output being trusted on faith.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from .types import ClipContext, Hook, HookType, CtrEstimate

log = logging.getLogger(__name__)


class HookWriter(Protocol):
    def write(self, context: ClipContext, count: int) -> list[Hook]:
        ...


class NullWriter:
    """No-op writer. Templates only."""

    def write(self, context: ClipContext, count: int) -> list[Hook]:
        return []


BRIEF = """\
You write hooks for short-form vertical video — the text overlaid on the first \
frame. A hook has one job: make the next second feel mandatory. It is not a \
title, not a summary, and not a sentence about the clip.

Rules:
- Four to nine words. Past twelve nobody finishes reading it.
- Front-load the strongest word. Viewers read left to right and stop early.
- Be specific. "I lost a lot" is nothing; "I lost $18 million" is everything. \
Use figures, names and concrete nouns from the transcript — never invent one.
- Loss framing beats gain framing. What someone stands to lose outperforms \
what they stand to gain.
- No engagement bait ("you won't believe", "wait for it", "gone wrong"). \
Audiences discount it and platforms suppress it.
- Never promise something the clip does not deliver. A hook that wins the \
click and loses the watch-through is worse than no hook.

Write hooks across these types, staying honest to what the clip contains:
curiosity, controversy, authority, fear, surprise, number, question, \
transformation, negativity, social_proof."""


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [t.value for t in HookType],
                    },
                    "grounded_in": {"type": "string"},
                },
                "required": ["text", "type", "grounded_in"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["hooks"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class AnthropicWriter:
    """Hook writer backed by the Claude API."""

    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 8_000
    use_refusal_fallback: bool = True
    client: Any = None

    def __post_init__(self) -> None:
        if self.client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "The `anthropic` package is required for AnthropicWriter. "
                    "Install it, or run the engine without an LLM writer for "
                    "template-only hooks."
                ) from exc
            self.client = anthropic.Anthropic()

    def write(self, context: ClipContext, count: int) -> list[Hook]:
        try:
            return self._write(context, count)
        except Exception:
            # Degrade to templates rather than failing the clip.
            log.exception("LLM hook writing failed; falling back to templates")
            return []

    def _write(self, context: ClipContext, count: int) -> list[Hook]:
        signals = ", ".join(context.signals) if context.signals else "none detected"
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": BRIEF,
                    # Byte-identical across every clip, so it caches once.
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
                        f"Write {count} hooks for this clip. Detected content "
                        f"signals: {signals}.\n\n"
                        "For each hook, `grounded_in` must quote the phrase from "
                        "the transcript the hook is based on, so an unfounded "
                        "claim is visible rather than plausible.\n\n"
                        f"Transcript:\n{context.text}"
                    ),
                }
            ],
        }

        response = self._create(request)

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            log.warning(
                "LLM declined to write hooks (category=%s); using templates",
                getattr(details, "category", None),
            )
            return []

        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        return self._parse(text, context) if text else []

    def _create(self, request: dict[str, Any]) -> Any:
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
                if "fallback" not in str(exc).lower():
                    raise
                log.debug("Provider rejected `fallbacks`; using stable endpoint")
        return self.client.messages.create(**request)

    @staticmethod
    def _parse(text: str, context: ClipContext) -> list[Hook]:
        data = json.loads(text)
        hooks: list[Hook] = []
        haystack = context.text.lower()

        for row in data.get("hooks", []):
            raw = str(row.get("text", "")).strip()
            if not raw:
                continue
            try:
                hook_type = HookType(row["type"])
            except (KeyError, ValueError):
                log.debug("LLM returned unknown hook type %r", row.get("type"))
                continue

            # Cheap grounding check: the quoted evidence has to actually exist
            # in the transcript. Catches the failure mode where a hook invents
            # a figure that would be indistinguishable from a real one.
            grounded = str(row.get("grounded_in", "")).strip().lower()
            if grounded and len(grounded) > 8 and grounded not in haystack:
                log.debug("dropping ungrounded hook %r (claimed: %r)", raw, grounded)
                continue

            hooks.append(
                Hook(
                    text=raw,
                    hook_type=hook_type,
                    # Replaced by the shared estimator; LLM hooks are not
                    # trusted more than template hooks.
                    estimate=CtrEstimate(lift=1.0, ctr=0.0, baseline=0.0),
                    source="llm",
                )
            )

        return hooks
