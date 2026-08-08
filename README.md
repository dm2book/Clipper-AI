# ClipForge AI

Turn long-form content into short-form vertical clips, automatically.

**Sources:** YouTube · Twitch · Kick · Podcasts · Direct upload · Livestream recordings
**Destinations:** TikTok · YouTube Shorts · Instagram Reels

## Status

Early implementation. The system design is complete; the first component — the
viral detection engine — is built and tested. No ingest, render, or publishing
code yet.

- [System architecture](docs/ARCHITECTURE.md) — data model, API, workers,
  upload path, processing pipeline, capacity model, delivery phases.
- **Viral detection engine** (`src/clipforge/viral/`) — stage 4 of the
  processing pipeline: transcript in, ranked clips out.

## Quick start

No dependencies and no credentials needed for the heuristic path:

```bash
python demo/run_demo.py            # ranked clips from the sample transcript
python demo/run_demo.py --json     # machine-readable output
python -m unittest discover -s tests -t tests
```

```python
from clipforge.viral import ViralDetectionEngine, load_json

result = ViralDetectionEngine().detect(load_json("episode.json"))
for clip in result.top:
    print(clip.scores.virality, clip.title)
```

## The detection engine

Detects ten categories of moment — controversy, emotional spikes, money
topics, funny moments, arguments, debates, failures, success stories, secrets,
and lessons — and returns five scores per clip: **virality, engagement,
retention, comment, share**.

```
transcript
  → detect signals             cheap, every utterance, tuned for recall
  → generate candidate windows anchored on signal hits, snapped to utterances
  → triage (fast LLM pass)     optional
  → deep judge (strong pass)   optional, top N only
  → score                      signals × affinity matrix, damped by structure
  → dedupe + diversify         NMS on time overlap, then spread across source
  → top clips
```

Two design points worth knowing before reading the code:

**Virality is not the average of the other four.** It is weighted 40% retention,
25% share, 20% engagement, 15% comment, because short-form ranking systems
reward watch-through and amplification far above comments. A clip everyone
argues about but nobody finishes does not travel.

**The engine runs fine with no LLM.** The heuristic tier locates moments; the
LLM tier judges them. `NullJudge` is the default, which keeps the test suite
offline and gives the product a degraded-but-working mode when the provider is
down. Enable the cascade explicitly:

```python
from clipforge.viral import ViralConfig, ViralDetectionEngine, build_default_judges

triage, deep = build_default_judges()
engine = ViralDetectionEngine(ViralConfig(triage_judge=triage, deep_judge=deep))
```

### Layout

| Module | Responsibility |
|---|---|
| `taxonomy.py` | Signal→behaviour affinity matrix, virality mix, duration model. The engine's opinion about what performs — versioned, and the file to edit when tuning. |
| `detectors.py` | The ten detectors. Lexical and structural; tuned for recall. |
| `candidates.py` | Window generation, anchored on hits and snapped to utterance boundaries. |
| `features.py` | Structural features — hook, standalone comprehensibility, payoff, duration fit. |
| `scoring.py` | Signals + features → the five scores. |
| `llm.py` | Semantic judgement tier. Protocol + Claude adapter + `NullJudge`. |
| `ranking.py` | Non-maximum suppression, diversity, final selection. |
| `engine.py` | Orchestration and configuration. |

Weights are versioned (`taxonomy.WEIGHTS_VERSION`) and persisted with every
moment, alongside the full feature vector. That is deliberate: the ranker is
heuristic today and is meant to be replaced by a model trained on real platform
performance, which needs to know what the heuristic ranker believed at decision
time — including about clips it rejected.

## Reading order

Start with the [design principles](docs/ARCHITECTURE.md#1-design-principles)
and [system overview](docs/ARCHITECTURE.md#2-system-overview). They establish
the control-plane/data-plane split the rest of the document depends on.

If you are picking up implementation work, the sections that constrain early
code most are the [database architecture](docs/ARCHITECTURE.md#3-database-architecture)
(tenancy and indexing decisions that are expensive to change later) and the
[worker architecture](docs/ARCHITECTURE.md#5-worker-architecture)
(idempotency and checkpointing requirements).
