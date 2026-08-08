"""ClipForge AI — viral moment detection.

Turns a transcript into a ranked set of short-form clip candidates.

    from clipforge.viral import ViralDetectionEngine, load_json

    transcript = load_json("episode.json")
    result = ViralDetectionEngine().detect(transcript)
    for clip in result.top:
        print(clip.scores.virality, clip.title)

Detection runs on heuristics alone by default. To enable the LLM cascade:

    from clipforge.viral import ViralConfig, ViralDetectionEngine, build_default_judges

    triage, deep = build_default_judges()
    engine = ViralDetectionEngine(ViralConfig(triage_judge=triage, deep_judge=deep))
"""

from .engine import ViralConfig, ViralDetectionEngine, build_default_judges
from .llm import AnthropicJudge, MomentJudge, NullJudge
from .transcript import from_utterances, from_words, load_json
from .types import (
    Candidate,
    DetectionResult,
    LlmVerdict,
    Moment,
    Scores,
    Signal,
    SignalHit,
    Transcript,
    Utterance,
    Word,
)

__all__ = [
    "AnthropicJudge",
    "Candidate",
    "DetectionResult",
    "LlmVerdict",
    "Moment",
    "MomentJudge",
    "NullJudge",
    "Scores",
    "Signal",
    "SignalHit",
    "Transcript",
    "Utterance",
    "ViralConfig",
    "ViralDetectionEngine",
    "Word",
    "build_default_judges",
    "from_utterances",
    "from_words",
    "load_json",
]

__version__ = "0.1.0"
