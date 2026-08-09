"""Hook features and the CTR estimator.

### What this actually is

There is no trained model here, because there is no click data to train one
on. Calling the output a "CTR prediction" without saying that would be
fabricated precision — the number would look authoritative and mean nothing.

What the engine does instead is estimate **relative lift between hooks for the
same clip**, from features that are well established in short-form copywriting
and direct-response advertising: specificity beats vagueness, loss framing
beats gain framing, curiosity gaps outperform statements, and there is a
length band beyond which overlay text stops being read at all.

`CtrEstimate.lift` is the model's real output. `CtrEstimate.ctr` is that lift
projected onto a caller-supplied baseline, purely so the number is legible to
a human — it inherits all of the baseline's error. Every estimate is stamped
`confidence="prior"`.

### How it stops being a prior

`HookSet.feature_rows()` emits one row per hook, including the ones that lost,
with the full feature vector and the weights version that produced the
ranking. Join those to impressions and clicks and the hand-tuned weights below
are replaced by fitted ones. The losing rows are the important half: a model
trained only on hooks that shipped learns which hooks get chosen, not which
hooks work.
"""

from __future__ import annotations

import re

from .types import Hook, HookType, CtrEstimate, clamp

WEIGHTS_VERSION = "hook-heuristic-v1"

# Platform baselines for the *projection only*. These are order-of-magnitude
# placeholders for legibility, not measurements — pass the channel's own
# number via `HookConfig.baseline_ctr` and these become irrelevant.
DEFAULT_BASELINE_CTR = 5.0

# Bounds on estimated lift. Deliberately narrow: a hook rewrite moves
# click-through by a meaningful fraction, not by an order of magnitude, and a
# model that claims 4x from a copy change is not one to trust.
LIFT_MIN = 0.60
LIFT_MAX = 1.80


# --- Feature vocabularies ----------------------------------------------------

_CURIOSITY_MARKERS = re.compile(
    r"\b(?:why|what|how|the reason|nobody|never told|secret|actually|really|"
    r"turns out|found out|hidden|behind|truth|until|before you)\b",
    re.IGNORECASE,
)

# Words with high emotional charge. Not "power words" in the SEO-spam sense —
# these are terms that carry consequence.
_POWER_WORDS = re.compile(
    r"\b(?:lost|cost|ruined|destroyed|failed|broke|killed|wasted|collapsed|"
    r"stop|never|worst|biggest|hardest|brutal|insane|proof|mistake|warning|"
    r"quit|fired|bankrupt|regret|disaster)\b",
    re.IGNORECASE,
)

_SECOND_PERSON = re.compile(r"\b(?:you|your|you're|yourself)\b", re.IGNORECASE)

_NEGATIVE_FRAME = re.compile(
    r"\b(?:don't|never|stop|lose|lost|mistake|wrong|fail|failed|worst|"
    r"ruins|ruined|too late|nobody|not)\b",
    re.IGNORECASE,
)

_NOVELTY = re.compile(
    r"\b(?:nobody|no one|never|first|only|unpopular|nobody's|rarely)\b",
    re.IGNORECASE,
)

_NUMBER = re.compile(r"[$€£]?\d|(?:\bmillion\b|\bbillion\b|\bthousand\b)", re.IGNORECASE)

# Phrasings that read as engagement-bait and are widely down-weighted by
# platforms and by audiences. Present in the bank on purpose (some still
# perform) but penalised so they rank below honest phrasing.
_BAIT = re.compile(
    r"\b(?:you won't believe|shocking|gone wrong|number \d+ will|"
    r"doctors hate|one weird trick|must see|going viral)\b",
    re.IGNORECASE,
)

_VAGUE = re.compile(
    r"\b(?:things?|stuff|something|anything|somehow|kind of|sort of|a lot)\b",
    re.IGNORECASE,
)

# Overlay text stops being read past roughly this point at phone size. The
# band is where hooks should live, not a hard limit.
IDEAL_WORDS = (4, 9)
MAX_WORDS = 14


def _length_fit(word_count: int) -> float:
    """1.0 inside the readable band, tapering outside it."""
    low, high = IDEAL_WORDS
    if low <= word_count <= high:
        return 1.0
    if word_count < low:
        # Very short can be strong ("$18 million.") so the taper is gentle.
        return clamp(0.55 + 0.15 * word_count)
    if word_count >= MAX_WORDS:
        return 0.15
    return clamp(1.0 - (word_count - high) / (MAX_WORDS - high) * 0.8)


def _front_load(text: str) -> float:
    """Whether the strongest content is in the first three words.

    A hook is read left to right and abandoned early. Burying the number or
    the power word at the end wastes the only part reliably seen.
    """
    opening = " ".join(text.split()[:3])
    if not opening:
        return 0.0
    score = 0.0
    if _NUMBER.search(opening):
        score += 0.6
    if _POWER_WORDS.search(opening):
        score += 0.5
    if _CURIOSITY_MARKERS.search(opening):
        score += 0.35
    return clamp(score)


def extract_features(text: str) -> dict[str, float]:
    """The feature vector for one hook.

    Persisted verbatim with the hook so that a future fitted model trains on
    exactly the inputs the heuristic ranker saw.
    """
    words = text.split()
    word_count = len(words)
    letters = [c for c in text if c.isalpha()]

    return {
        "curiosity_gap": clamp(len(_CURIOSITY_MARKERS.findall(text)) * 0.45),
        "specificity": clamp(len(_NUMBER.findall(text)) * 0.55),
        "length_fit": _length_fit(word_count),
        "power_words": clamp(len(_POWER_WORDS.findall(text)) * 0.40),
        "second_person": 1.0 if _SECOND_PERSON.search(text) else 0.0,
        "negative_frame": clamp(len(_NEGATIVE_FRAME.findall(text)) * 0.45),
        "novelty": clamp(len(_NOVELTY.findall(text)) * 0.50),
        "front_load": _front_load(text),
        # Short words read faster; a hook full of long words is a sentence.
        "readability": clamp(
            1.0 - (sum(len(w) for w in words) / max(1, word_count) - 4.0) / 6.0
        ),
        "shouting": (
            sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0
        ),
    }


# Feature weights. Positive features only — penalties are applied separately
# as multipliers so they are visible in the output rather than buried in a sum.
_WEIGHTS: dict[str, float] = {
    "curiosity_gap": 0.20,
    "specificity": 0.18,
    "length_fit": 0.16,
    "power_words": 0.12,
    "negative_frame": 0.11,   # loss framing beats gain framing, consistently
    "front_load": 0.10,
    "novelty": 0.07,
    "second_person": 0.04,
    "readability": 0.02,
}


def _penalties(text: str, features: dict[str, float]) -> tuple[float, tuple[str, ...]]:
    """Multiplier and reasons for anything that should push a hook down."""
    multiplier = 1.0
    reasons: list[str] = []

    if _BAIT.search(text):
        multiplier *= 0.62
        reasons.append("engagement-bait phrasing")

    if _VAGUE.search(text):
        multiplier *= 0.85
        reasons.append("vague wording")

    # All-caps overlay text reads as shouting and is heavily discounted by
    # audiences; the caption engine already handles case, so a hook arriving
    # pre-shouted is a copy problem, not a styling choice.
    if features.get("shouting", 0.0) > 0.7 and len(text) > 12:
        multiplier *= 0.80
        reasons.append("all caps")

    exclamations = text.count("!")
    if exclamations >= 2:
        multiplier *= 0.82
        reasons.append("excessive punctuation")

    if len(text.split()) > MAX_WORDS:
        multiplier *= 0.70
        reasons.append("too long to read")

    emoji_count = sum(1 for c in text if ord(c) > 0x2100)
    if emoji_count > 1:
        multiplier *= 0.88
        reasons.append("multiple emoji")

    return multiplier, tuple(reasons)


def estimate(
    text: str,
    template_weight: float = 1.0,
    baseline_ctr: float = DEFAULT_BASELINE_CTR,
) -> tuple[CtrEstimate, dict[str, float], tuple[str, ...]]:
    """Estimate relative lift for one hook.

    Returns the estimate, its feature vector, and any penalties applied.
    """
    features = extract_features(text)

    raw = sum(features.get(name, 0.0) * weight for name, weight in _WEIGHTS.items())
    total_weight = sum(_WEIGHTS.values())
    normalised = clamp(raw / total_weight) if total_weight else 0.0

    penalty, reasons = _penalties(text, features)

    # The template prior nudges ties without being able to override features:
    # a strong pattern filled with weak content should still lose to a weak
    # pattern filled with a dollar figure.
    prior = clamp(template_weight / 1.25, 0.6, 1.0)
    combined = clamp(normalised * (0.85 + 0.15 * prior)) * penalty

    lift = LIFT_MIN + (LIFT_MAX - LIFT_MIN) * combined
    return (
        CtrEstimate(
            lift=round(lift, 4),
            ctr=round(baseline_ctr * lift, 3),
            baseline=baseline_ctr,
            confidence="prior",
        ),
        features,
        reasons,
    )


def type_affinity(hook_type: HookType, signals: tuple[str, ...]) -> float:
    """How well a hook type suits a clip's detected content, 0.5..1.2.

    A fear hook on a comedy clip is a mismatch no amount of good copy fixes:
    the hook promises one thing and the clip delivers another, which costs
    watch-through even when it wins the click. Signal names are the viral and
    stream engines' vocabularies, so this composes with both.
    """
    if not signals:
        return 1.0

    affinity: dict[HookType, set[str]] = {
        HookType.CURIOSITY: {"secret", "reaction", "emotional_spike"},
        HookType.CONTROVERSY: {"controversy", "argument", "debate", "rage"},
        HookType.AUTHORITY: {"lesson", "success", "money", "win"},
        HookType.FEAR: {"failure", "fail", "warning", "money"},
        HookType.SURPRISE: {"emotional_spike", "reaction", "secret", "fail"},
        HookType.NUMBER: {"money", "success", "win"},
        HookType.QUESTION: {"controversy", "debate", "argument"},
        HookType.TRANSFORMATION: {"success", "failure", "fail", "money"},
        HookType.NEGATIVITY: {"failure", "fail", "lesson", "rage"},
        HookType.SOCIAL_PROOF: {"secret", "controversy", "lesson"},
    }

    matched = affinity.get(hook_type, set()) & set(signals)
    if matched:
        return clamp(1.0 + 0.10 * len(matched), 1.0, 1.2)
    return 0.82


def score_hook(
    hook: Hook, signals: tuple[str, ...], baseline_ctr: float
) -> Hook:
    """Attach a signal-adjusted estimate to a hook, in place."""
    estimate_result, features, reasons = estimate(
        hook.text, baseline_ctr=baseline_ctr
    )
    affinity = type_affinity(hook.hook_type, signals)
    adjusted_lift = clamp(estimate_result.lift * affinity, LIFT_MIN, LIFT_MAX)

    hook.estimate = CtrEstimate(
        lift=round(adjusted_lift, 4),
        ctr=round(baseline_ctr * adjusted_lift, 3),
        baseline=baseline_ctr,
        confidence="prior",
    )
    hook.features = {**features, "type_affinity": affinity}
    hook.penalties = reasons
    return hook
