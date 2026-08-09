"""ClipForge AI — analytics intelligence.

Tracks views, retention, likes, comments, shares and subscribers; answers the
five questions the product promises; and produces weekly reports on a schedule.

    from clipforge.analytics import AnalyticsEngine

    engine = AnalyticsEngine()
    engine.track(record)
    engine.ingest(source)
    print(engine.report(week_end).render())

This is the engine every other one was built for. The viral ranker, the hook
estimator and the factory have each been persisting a feature vector and a
weights version with every decision, explicitly so that outcomes could later be
joined back to them. `calibration()` is what that was for: it checks whether
the hand-tuned priors actually predict anything, and says plainly when they do
not.

**The hard part is not the arithmetic, it is refusing to overclaim.** A ranking
always has a top row, and presenting that row as a finding is how a creator
reorganises their week around three posts of noise. So every comparison carries
a minimum sample, a distribution-free significance test, false-discovery-rate
control across the whole family of comparisons, and — when nothing is
significant — the smallest effect the data *could* have detected, which turns
"we cannot tell" into "you need forty more posts".

**"Best hook" is only answerable if the system sometimes publishes a hook it
did not choose.** Otherwise every observed outcome is an outcome for a hook the
model already liked, and the analysis measures the model rather than the hooks.
`experiments.py` pays that cost deliberately, and `assess()` labels any
comparison that has not yet earned the right to be read as causal.
"""

from .attribution import (
    DIMENSIONS,
    METRICS,
    WEEKDAYS,
    AnalyticsStore,
    PostRecord,
    dimension_value,
)
from .engine import (
    AnalyticsConfig,
    AnalyticsEngine,
    MetricSource,
    RecordedSource,
)
from .experiments import (
    Assignment,
    DEFAULT_EXPLORE_RATE,
    EXPLORE_DEPTH,
    ExplorationPolicy,
    MIN_EXPLORED,
    Validity,
    assess,
)
from .insights import (
    Calibration,
    Insight,
    RetentionDiagnosis,
    analyse,
    best_clip_lengths,
    best_creators,
    best_hooks,
    best_posting_times,
    best_topics,
    calibration,
    diagnose_retention,
)
from .metrics import (
    Baselines,
    CHECKPOINTS_H,
    DEFAULT_BASELINES,
    PRIMARY_CHECKPOINT_H,
    PostMetrics,
    RetentionCurve,
    Snapshot,
)
from .report import Delta, WeeklyReport, build_weekly
from .stats import (
    Comparison,
    DEFAULT_FDR,
    GroupResult,
    MIN_GROUP_N,
    MIN_MATERIAL_EFFECT,
    benjamini_hochberg,
    bootstrap_ci,
    compare,
    mean,
    median,
    minimum_detectable_effect,
    permutation_p,
    samples_needed,
    stdev,
    trimmed_mean,
)

__all__ = [
    "AnalyticsConfig",
    "AnalyticsEngine",
    "AnalyticsStore",
    "Assignment",
    "Baselines",
    "CHECKPOINTS_H",
    "Calibration",
    "Comparison",
    "DEFAULT_BASELINES",
    "DEFAULT_EXPLORE_RATE",
    "DEFAULT_FDR",
    "DIMENSIONS",
    "Delta",
    "EXPLORE_DEPTH",
    "ExplorationPolicy",
    "GroupResult",
    "Insight",
    "METRICS",
    "MIN_EXPLORED",
    "MIN_GROUP_N",
    "MIN_MATERIAL_EFFECT",
    "MetricSource",
    "PRIMARY_CHECKPOINT_H",
    "PostMetrics",
    "PostRecord",
    "RecordedSource",
    "RetentionCurve",
    "RetentionDiagnosis",
    "Snapshot",
    "Validity",
    "WEEKDAYS",
    "WeeklyReport",
    "analyse",
    "assess",
    "benjamini_hochberg",
    "best_clip_lengths",
    "best_creators",
    "best_hooks",
    "best_posting_times",
    "best_topics",
    "bootstrap_ci",
    "build_weekly",
    "calibration",
    "compare",
    "diagnose_retention",
    "dimension_value",
    "mean",
    "median",
    "minimum_detectable_effect",
    "permutation_p",
    "samples_needed",
    "stdev",
    "trimmed_mean",
]
