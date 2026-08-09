"""ClipForge AI — hook generation.

Twenty ranked hook variations per clip, across ten psychological types, each
with an estimated click-through lift.

    from clipforge.hooks import generate

    hooks = generate(clip_text, signals=("money", "failure"))
    for hook in hooks.hooks[:5]:
        print(hook.estimate.percent, hook.hook_type.value, hook.text)

**On the CTR numbers.** There is no trained model here, because there is no
click data to train one on. The engine estimates *relative lift between hooks
for the same clip* from established short-form copywriting features, then
projects that lift onto a baseline you supply so the number is legible. Every
estimate is stamped `confidence="prior"`. `HookSet.feature_rows()` emits the
training table — including the hooks that lost — so the hand-tuned weights can
be replaced by fitted ones once real impressions exist.
"""

from .engine import HookConfig, HookGenerator, generate
from .extraction import extract
from .llm import AnthropicWriter, HookWriter, NullWriter
from .scoring import (
    DEFAULT_BASELINE_CTR,
    LIFT_MAX,
    LIFT_MIN,
    WEIGHTS_VERSION,
    estimate,
    extract_features,
    type_affinity,
)
from .templates import BANK, Template, for_language, supported_languages
from .types import (
    CORE_TYPES,
    ClipContext,
    CtrEstimate,
    Hook,
    HookSet,
    HookType,
    Slots,
)

__all__ = [
    "AnthropicWriter",
    "BANK",
    "CORE_TYPES",
    "ClipContext",
    "CtrEstimate",
    "DEFAULT_BASELINE_CTR",
    "Hook",
    "HookConfig",
    "HookGenerator",
    "HookSet",
    "HookType",
    "HookWriter",
    "LIFT_MAX",
    "LIFT_MIN",
    "NullWriter",
    "Slots",
    "Template",
    "WEIGHTS_VERSION",
    "estimate",
    "extract",
    "extract_features",
    "for_language",
    "generate",
    "supported_languages",
    "type_affinity",
]
