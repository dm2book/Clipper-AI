"""The hook generator — orchestration.

    clip text (+ optional signals from the viral or stream engines)
      → extract slots (numbers, topic, outcome, timeframe, quote)
      → render every template whose slots are fillable
      → optionally add LLM-written hooks
      → score all candidates with the same estimator
      → dedupe near-identical phrasings
      → enforce type diversity
      → rank, return the top N

Runs offline by default and always returns a full set.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Sequence

from . import extraction, scoring, templates as template_mod
from .llm import HookWriter, NullWriter
from .types import (
    ClipContext,
    CORE_TYPES,
    CtrEstimate,
    Hook,
    HookSet,
    HookType,
    Slots,
)

_NORMALISE = re.compile(r"[^a-z0-9 ]+")


@dataclass(slots=True)
class HookConfig:
    """Tuning surface for one generation run."""

    #: How many hooks to return. Twenty is the product default.
    count: int = 20

    #: Baseline CTR the lift is projected onto, as a percentage. Pass the
    #: channel's own measured number — the default is a legibility placeholder,
    #: not a measurement, and every projected CTR inherits its error.
    baseline_ctr: float = scoring.DEFAULT_BASELINE_CTR

    #: Cap per hook type, so twenty hooks are not twenty curiosity hooks. With
    #: the default the set spans at least five types.
    max_per_type: int = 4

    #: Guarantee at least one hook from each of the five core types when the
    #: clip's content supports it.
    guarantee_core_types: bool = True

    #: Two hooks sharing this much normalised text are the same hook.
    dedupe_threshold: float = 0.72

    #: Optional LLM writer. Off by default.
    writer: HookWriter = field(default_factory=NullWriter)

    #: How many hooks to request from the LLM when one is configured.
    llm_count: int = 12


class HookGenerator:
    """Generates, scores and ranks hooks for a clip."""

    def __init__(self, config: HookConfig | None = None) -> None:
        self.config = config or HookConfig()

    def generate(self, context: ClipContext | str) -> HookSet:
        started = time.perf_counter()
        cfg = self.config

        if isinstance(context, str):
            context = ClipContext(text=context)

        if not context.text.strip():
            return HookSet(
                hooks=[],
                slots=Slots(),
                weights_version=scoring.WEIGHTS_VERSION,
                stats={"reason": "empty clip text"},
            )

        slots = extraction.extract(context)

        bank = template_mod.for_language(context.language)
        language_supported = context.language in template_mod.supported_languages()

        candidates: list[Hook] = []
        for template in bank:
            text = template_mod.render(template, slots)
            if text is None:
                continue
            candidates.append(
                Hook(
                    text=text,
                    hook_type=template.hook_type,
                    estimate=CtrEstimate(lift=1.0, ctr=0.0, baseline=cfg.baseline_ctr),
                    source="template",
                    template_id=template.id,
                )
            )

        llm_hooks = self._write_llm(context)
        candidates.extend(llm_hooks)

        for hook in candidates:
            scoring.score_hook(hook, context.signals, cfg.baseline_ctr)

        # Template priors are applied after scoring so they break ties without
        # being able to outrank a genuinely stronger hook.
        priors = {t.id: t.weight for t in bank}
        candidates.sort(
            key=lambda h: (-h.estimate.lift, -priors.get(h.template_id, 1.0), h.text)
        )

        deduped = self._dedupe(candidates, cfg.dedupe_threshold)
        selected = self._select(deduped, cfg)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return HookSet(
            hooks=selected,
            slots=slots,
            weights_version=scoring.WEIGHTS_VERSION,
            stats={
                "requested": cfg.count,
                "returned": len(selected),
                "templates_rendered": len(candidates) - len(llm_hooks),
                "llm_hooks": len(llm_hooks),
                "after_dedupe": len(deduped),
                "types_covered": len({h.hook_type for h in selected}),
                "baseline_ctr": cfg.baseline_ctr,
                "slots_filled": sum(1 for v in slots.as_dict().values() if v),
                "language": context.language,
                "language_supported": language_supported,
                "elapsed_ms": elapsed_ms,
            },
        )

    # -- internals -----------------------------------------------------------

    def _write_llm(self, context: ClipContext) -> list[Hook]:
        writer = self.config.writer
        if isinstance(writer, NullWriter):
            return []
        try:
            return writer.write(context, self.config.llm_count)
        except Exception:
            return []

    @staticmethod
    def _key(text: str) -> set[str]:
        return set(_NORMALISE.sub("", text.lower()).split())

    def _dedupe(self, hooks: Sequence[Hook], threshold: float) -> list[Hook]:
        """Drop hooks that are near-restatements of a better-scoring one.

        Templates overlap by design — several curiosity patterns collapse to
        nearly the same sentence once the same topic is substituted in. Twenty
        hooks that are four hooks in disguise is a worse deliverable than
        fifteen distinct ones.
        """
        kept: list[Hook] = []
        keys: list[set[str]] = []

        for hook in hooks:
            key = self._key(hook.text)
            if not key:
                continue
            duplicate = False
            for existing in keys:
                union = key | existing
                if not union:
                    continue
                if len(key & existing) / len(union) >= threshold:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(hook)
                keys.append(key)

        return kept

    def _select(self, hooks: Sequence[Hook], cfg: HookConfig) -> list[Hook]:
        """Pick the final set, respecting the per-type cap and core coverage."""
        chosen: list[Hook] = []
        per_type: dict[HookType, int] = {}

        # First pass: one from each core type, so the set always demonstrates
        # the five levers the product advertises.
        if cfg.guarantee_core_types:
            for hook_type in CORE_TYPES:
                best = next((h for h in hooks if h.hook_type is hook_type), None)
                if best is not None and len(chosen) < cfg.count:
                    chosen.append(best)
                    per_type[hook_type] = 1

        # Second pass: fill by score, honouring the per-type cap.
        for hook in hooks:
            if len(chosen) >= cfg.count:
                break
            if hook in chosen:
                continue
            if per_type.get(hook.hook_type, 0) >= cfg.max_per_type:
                continue
            chosen.append(hook)
            per_type[hook.hook_type] = per_type.get(hook.hook_type, 0) + 1

        # Third pass: if the cap starved the set, relax it rather than
        # returning fewer hooks than asked for.
        if len(chosen) < cfg.count:
            for hook in hooks:
                if len(chosen) >= cfg.count:
                    break
                if hook not in chosen:
                    chosen.append(hook)

        chosen.sort(key=lambda h: (-h.estimate.lift, h.text))
        return chosen


def generate(
    text: str,
    signals: Sequence[str] = (),
    count: int = 20,
    baseline_ctr: float = scoring.DEFAULT_BASELINE_CTR,
) -> HookSet:
    """Convenience wrapper for the common case."""
    return HookGenerator(
        HookConfig(count=count, baseline_ctr=baseline_ctr)
    ).generate(ClipContext(text=text, signals=tuple(signals)))
