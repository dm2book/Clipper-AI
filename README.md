# ClipForge AI

Turn long-form content into short-form vertical clips, automatically.

**Sources:** YouTube · Twitch · Kick · Podcasts · Direct upload · Livestream recordings
**Destinations:** TikTok · YouTube Shorts · Instagram Reels

## Status

Early implementation. The system design is complete; three engines are
built and tested. No ingest, render, or publishing code yet.

- [System architecture](docs/ARCHITECTURE.md) — data model, API, workers,
  upload path, processing pipeline, capacity model, delivery phases.
- **Viral detection engine** (`src/clipforge/viral/`) — long-form transcript
  in, ranked clips out. For podcasts, interviews, uploaded video.
- **Stream clipper engine** (`src/clipforge/stream/`) — Twitch, Kick and
  YouTube Live chat in, vertical 15/30/45/60s clips out.
- **Caption engine** (`src/clipforge/captions/`) — word-level animated
  captions in five languages, exported to ASS / VTT / SRT / JSON.

## Quick start

No dependencies and no credentials needed:

```bash
python demo/run_demo.py            # ranked clips from a sample transcript
python demo/run_stream_demo.py     # vertical clips from a sample Twitch VOD
python demo/run_stream_demo.py --all --json
python demo/run_caption_demo.py           # captions in all five languages
python demo/run_caption_demo.py --styles  # compare the five presets
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

## The stream clipper

Twitch, Kick, and YouTube Live. Detects rage, funny moments, wins, fails,
reactions, donations, arguments, and emotional moments, then cuts each into
15/30/45/60-second vertical clips.

```
chat + events + optional transcript + scene regions
  → classify chat against the emote taxonomy
  → find chat spikes against a rolling median baseline
  → LAG-CORRECT the spike onsets into anchors
  → merge, then cut 15/30/45/60s around each
  → score every length, suppress overlaps
  → vertical layout plan per destination
```

**Chat lag is the whole problem.** Between something happening on screen and a
message appearing in chat there is broadcast latency, then human reaction, then
typing — four to five seconds on Twitch, six or more on YouTube Live. A clipper
that centres on the chat spike starts *after* the moment and captures only the
aftermath. So the pipeline subtracts a platform-specific lag from the spike
**onset** (not its peak) before placing any window. On the sample VOD that
recovers all seven planted moments to within three seconds.

Three other decisions worth knowing:

**Signal strength is a share of chat, not a count.** A 500-message burst
contains a few of everything, so saturating over raw hits pins every signal at
1.0 and the engine can no longer tell a rage moment from a donation. Measuring
the *fraction* of chat carrying each signal restores discrimination and is
viewer-count independent — the same reason spike detection works on ratios.

**All four lengths are always cut, and the engine says which is best.** A
headshot needs fifteen seconds; an argument needs a minute, and cutting it to
fifteen produces a clip where two people are inexplicably annoyed. Fast signals
(win/fail/funny) prefer short, slow ones (argument/emotional/donation) prefer
long.

**Vertical is a composition, not a centre crop.** A 16:9 frame cropped to 9:16
throws away either the gameplay or the streamer's reaction. The planner stacks
facecam above cropped gameplay and places captions clear of each destination's
UI chrome — TikTok's right rail, Reels' 20% bottom band. Getting safe zones
wrong is invisible in preview and wrong on every published clip.

| Module | Responsibility |
|---|---|
| `emotes.py` | Emote, emoji, and slang taxonomy → signal weights. Handles multi-signal tokens (OMEGALUL is laughter *at* a fail). |
| `adapters.py` | Twitch / Kick / YouTube Live normalisation. Kick needs a stream start; its timestamps are wall-clock. |
| `signals.py` | Chat bucketing, rolling median baseline, spike detection, share-based aggregation. |
| `anchors.py` | Lag correction, anchor merging, exact-duration window placement. |
| `layout.py` | 9:16 composition and per-destination safe zones. |
| `scoring.py` | hype / retention / clarity / virality, plus duration preference. |
| `engine.py` | Orchestration and configuration. |

## Reading order

Start with the [design principles](docs/ARCHITECTURE.md#1-design-principles)
and [system overview](docs/ARCHITECTURE.md#2-system-overview). They establish
the control-plane/data-plane split the rest of the document depends on.

If you are picking up implementation work, the sections that constrain early
code most are the [database architecture](docs/ARCHITECTURE.md#3-database-architecture)
(tenancy and indexing decisions that are expensive to change later) and the
[worker architecture](docs/ARCHITECTURE.md#5-worker-architecture)
(idempotency and checkpointing requirements).

## The caption engine

Word-by-word animated captions in the premium short-form style. English,
Dutch, German, French, and Spanish.

```python
from clipforge.captions import generate, to_ass

track = generate(words, style="punch", language="de")
open("captions.ass", "w").write(to_ass(track, PUNCH))   # ffmpeg -vf ass=…
```

**Word-level input is required, and the engine refuses without it.** Given
sentence-level subtitles there is nothing to karaoke, nowhere precise to place
an emoji, and no way to know which word is being spoken. Faking the timing
produces highlighting that visibly drifts, so the engine raises instead.

Five presets — `punch` (2 words, 96px, pop), `karaoke` (6 words, colour
sweep), `bounce`, `minimal`, `typewriter`. Grouping is the real difference
between them: one word at a time reads as high-energy, six reads as editorial.

### Language rules are not cosmetic

Getting these wrong produces output that looks fine in English and broken in
the other four:

| Language | Rule the engine implements |
|---|---|
| French | Narrow no-break space before `; : ! ?`. Elision (`l'`, `qu'`, `j'`) never splits across lines. A detached `?` merges back into its word rather than becoming its own cue. |
| Spanish | `¿` and `¡` glue forward to the clause they open. |
| German | Compound overflow is normal, so cues shrink to fit rather than clip. `ß` uppercases to `SS`, which is correct orthography. Lowercase styles are skipped — noun capitalisation is grammar, not styling. |
| Dutch | Same compound handling, plus the `'s` proclitic (`'s ochtends`) held as one unit. |
| English | Orphaned articles kept off line ends. |

Emoji are chosen from a concept lexicon with per-language triggers — not a
translated English list — and matching sees through French elision
(`l'argent` → `argent`) and Romance verb inflection (`celebrando` →
`celebrar`). One emoji per cue, minimum two cues apart; back-to-back emoji is
the clearest tell that captions were machine-generated.

### Export

| Format | Carries |
|---|---|
| JSON | Everything, including per-word animation keyframes. The native render spec. |
| ASS | `\kf` karaoke fill, styling, speaker tint, per-cue scaling. `ffmpeg -vf ass=file.ass` burns it directly. |
| VTT | Inline `<timestamp>` tags for word-level highlighting in browsers. |
| SRT | Cue-level text only. Word timing does not survive the format. |

Motion is deliberately not in the ASS output: per-word scale animation needs
one positioned `Dialogue` per word and exact font metrics, which the engine
approximates rather than measures. ASS carries timing and colour; the JSON
carries motion.

| Module | Responsibility |
|---|---|
| `languages.py` | Per-language break rules, glue prefixes, typography, detection. |
| `emoji.py` | Concept lexicon with per-language triggers and stem matching. |
| `measure.py` | Text width without a font engine (±4% on Latin). |
| `chunking.py` | Punctuation merging, cue splitting, line breaking, balancing. |
| `animation.py` | Per-word keyframes for each animation style. |
| `styles.py` | The five presets and speaker palette. |
| `export.py` | ASS / VTT / SRT / JSON. |
| `engine.py` | Orchestration. |
