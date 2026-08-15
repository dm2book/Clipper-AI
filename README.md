# ClipForge AI

Turn long-form content into short-form vertical clips, automatically.

**Sources:** YouTube · Twitch · Kick · Podcasts · Direct upload · Livestream recordings
**Destinations:** TikTok · YouTube Shorts · Instagram Reels

## Status

Early implementation. The system design is complete; seven engines, the
channel factory that orchestrates them, the multi-tenant Empire layer on top,
and the persistence layer underneath are built and tested. Acquisition,
transcription and rendering now run end to end on a real file. The publishing
and analytics systems build and sequence platform API calls but still ship
with no live transport.

- [System architecture](docs/ARCHITECTURE.md) — data model, API, workers,
  upload path, processing pipeline, capacity model, delivery phases.
- **Viral detection engine** (`src/clipforge/viral/`) — long-form transcript
  in, ranked clips out. For podcasts, interviews, uploaded video.
- **Stream clipper engine** (`src/clipforge/stream/`) — Twitch, Kick and
  YouTube Live chat in, vertical 15/30/45/60s clips out.
- **Caption engine** (`src/clipforge/captions/`) — word-level animated
  captions in five languages, exported to ASS / VTT / SRT / JSON.
- **Hook generator** (`src/clipforge/hooks/`) — twenty ranked hook variations
  per clip across ten types, each with an estimated click-through lift.
- **Gameplay background engine** (`src/clipforge/gameplay/`) — speaker over a
  gameplay bed at 1080x1920 60fps, with auto-framing and an ffmpeg filtergraph.
- **Publishing system** (`src/clipforge/publish/`) — OAuth, recurring
  schedules, bulk uploads, a content calendar and a retrying worker loop for
  TikTok, YouTube and Instagram.
- **Channel factory** (`src/clipforge/factory/`) — create a channel from a
  niche; it finds content, clips it, hooks it, captions it, composes it and
  schedules it, independently of every other channel.
- **Analytics intelligence** (`src/clipforge/analytics/`) — tracks the six
  metrics, answers the five "best X" questions with confidence attached, and
  writes weekly reports.
- **Empire Mode** (`src/clipforge/empire/`) — tenants, brands, users and roles
  over 50+ channels, with capacity and unit economics computed rather than
  assumed.
- **Persistence** (`src/clipforge/store/`, `db/`) — PostgreSQL, Prisma
  migrations, row-level security, repositories. The engines' in-memory stores
  have durable equivalents, so nothing important is lost on restart.
- **Source acquisition** (`src/clipforge/acquire/`) — YouTube videos and
  channels, podcast RSS feeds and uploaded files in; resumable downloads,
  measured media and thumbnails out.
- **Rendering** (`src/clipforge/render/`) — executes the gameplay engine's
  filtergraph, burns captions in, and checks the output is what the plan
  asked for. Real 1080x1920 60fps MP4s.
- **Transcription** (`src/clipforge/transcribe/`) — media in, word-level
  timings out. Local Whisper or any OpenAI-compatible endpoint, chosen by
  environment variable; chunked so long media never lands in memory. The
  Whisper providers themselves are unverified here — see below.
- **Upload transport** (`src/clipforge/publish/transport.py`) — a real HTTP
  client for the three platforms: streamed chunk uploads, Google's `308`
  resume, token refresh, post-publish verification and account lifecycle. No
  upload has reached a live platform from here — see below.
- **Authentication** (`src/clipforge/auth/`) — email and password with real
  bcrypt, JWT sessions, rotating refresh tokens with reuse detection,
  verification and reset links, durable rate limiting and an append-only audit
  log. No email is sent — see below.
- **HTTP API** (`src/clipforge/api/`) — FastAPI over the stores, bearer-token
  authenticated, tenant taken from the token rather than the URL. The first
  runnable process in the repository.
- **Dashboard** (`web/`) — React + TypeScript, seven pages, types generated
  from the API's OpenAPI document. No mock data anywhere in it.
- **Media storage** (`src/clipforge/storage/`) — Cloudflare R2 over boto3, with
  a local backend behind the same interface: tenant-scoped keys, signed URLs,
  the public URLs Instagram needs, lifecycle rules, retries and metrics. No
  byte has reached Cloudflare from here — see below.

## Quick start

No dependencies and no credentials needed:

```bash
python demo/run_demo.py            # ranked clips from a sample transcript
python demo/run_stream_demo.py     # vertical clips from a sample Twitch VOD
python demo/run_stream_demo.py --all --json
python demo/run_caption_demo.py           # captions in all five languages
python demo/run_caption_demo.py --styles  # compare the five presets
python demo/run_hook_demo.py              # 20 ranked hooks for a sample clip
python demo/run_hook_demo.py --by-type --clip stream
python demo/run_gameplay_demo.py          # 1080x1920 60fps composition plan
python demo/run_gameplay_demo.py --all    # compare all five gameplay beds
python demo/run_gameplay_demo.py --camera --ffmpeg
python demo/run_publish_demo.py           # connect, schedule, publish
python demo/run_publish_demo.py --calendar --retry --dst
python demo/run_factory_demo.py           # seven channels, one cycle
python demo/run_factory_demo.py --niches --rights --quota --isolation
python demo/run_analytics_demo.py         # a weekly report
python demo/run_analytics_demo.py --honesty --retention --calibration
python demo/run_empire_demo.py            # 52 channels, 4 brands, one dashboard
python demo/run_empire_demo.py --capacity --economics --access --scale
python demo/run_render_demo.py --plan-only   # the filtergraph, no ffmpeg needed
python demo/run_render_demo.py --captions    # a real 1080x1920 60fps MP4
python demo/run_transcribe_demo.py --check   # configured providers, and which run
python demo/run_transcribe_demo.py --synthesise   # speech in, word timings out
python demo/run_upload_demo.py --all --verify    # real uploads over real sockets
python demo/run_auth_demo.py --all               # every auth flow, and its defences
python -m clipforge.api.server --in-memory       # the API, nothing persisted
PYTHONPATH=src python -m unittest discover -s tests -t tests
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

## The hook generator

Twenty ranked hook variations per clip — the text overlaid on the first frame,
whose only job is to make the next second feel mandatory.

```python
from clipforge.hooks import generate

hooks = generate(clip_text, signals=("failure", "money", "secret"))
for hook in hooks.hooks[:3]:
    print(hook.estimate.percent, hook.hook_type.value, hook.text)
```

```
7.5%  surprise    Fourteen million. That is what the raise actually cost.
7.3%  curiosity   What I have never told anyone about the raise
7.0%  curiosity   I found out why the raise lost
```

`signals` is the viral or stream engine's own vocabulary, so the three
compose: a clip the detector tagged `rage` gets controversy hooks weighted up
and authority hooks weighted down.

### There is no trained CTR model, and the API says so

This is the part worth reading before trusting a number.

Calling the output a "CTR prediction" would be fabricated precision — there is
no click data to fit a model on, so a number presented as a measurement would
look authoritative and mean nothing. What the engine actually estimates is
**relative lift between hooks for the same clip**, from features that are well
established in short-form copywriting: specificity beats vagueness, loss
framing beats gain framing, curiosity gaps outperform statements, and there is
a length band past which overlay text stops being read.

So the contract is explicit:

- `CtrEstimate.lift` is the model's real output, bounded to 0.60–1.80. A copy
  change moves click-through by a fraction, not an order of magnitude.
- `CtrEstimate.ctr` is that lift projected onto a baseline **you** supply, for
  legibility only. It inherits all of the baseline's error.
- `CtrEstimate.confidence` is `"prior"` on every hook this engine produces.
  The field exists so consumers are forced to notice when it stops being one.

`HookSet.feature_rows()` emits the training table — one row per hook with its
full feature vector and the weights version that ranked it. Join those to real
impressions and the hand-tuned weights are replaced by fitted ones. The rows
include the hooks that **lost**, which is the important half: a model trained
only on hooks that shipped learns which hooks get chosen, not which ones work.

### Ten types, five of them the product leads with

Curiosity, controversy, authority, fear and surprise are what the product
advertises, and the set is guaranteed to contain one of each when the clip
supports it. Number, question, transformation, negativity and social proof
exist because clips whose content does not suit the first five produce weak
hooks when forced into them.

Which lever works is not universal — a finance channel and a comedy channel
have opposite type rankings — so the type is persisted with every hook and
becomes measurable per creator once click data exists.

### Extraction is the whole game

"The real reason it failed" is a weak hook; "The real reason we lost $18
million" is a strong one, and the difference is entirely whether extraction
found the number. A template whose slots cannot be filled is skipped rather
than rendered with a placeholder — shipping a hook that reads `{number}` is
worse than shipping one fewer hook.

The extractor is deterministic and offline, and most of its complexity is
refusing to be confidently wrong:

| Failure it guards against | What goes wrong without it |
|---|---|
| Frequency as a proxy for subjecthood | The most frequent content word in a clip about a funding round is "months". |
| Units after a number | "fourteen **million**", "seven **months**" — never the subject. |
| Adjectives under a determiner | "a guaranteed win" is about the win, not the guarantee. |
| Complementizer `that` | "the lesson is *that* headcount…" yields the phrase "that headcount". |
| Adverbs | "the absolutely" — an adverb is never a head noun. |
| Hook vocabulary as topic | Picking "mistake" produces "The mistake mistake that ruins everything". |
| A single bare mention | One unremarkable noun is not evidence. Below the floor the topic is left empty. |

Bare noun and noun phrase are returned separately, because templates need
different ones: `The {topic} mistake` wants the bare noun, `after
{topic_phrase}` wants the determiner. Filling one from the other gives "The
the raise mistake" or "after raise". Past-tense outcomes carry a base form for
the same reason — "I did not expect the raise to **lost**" is what happens
without it.

**When extraction finds nothing, it says nothing.** Twenty-eight slotless
fallback templates carry the full set, so a clip with no extractable subject
returns twenty honest generic hooks rather than twenty hooks confidently
naming a word the clip is not about.

### The LLM tier is optional and competes on equal terms

Templates guarantee twenty hooks offline and are genuinely decent, but they
cannot reference the specific thing that makes a clip funny, and they reuse
phrasing across a creator's whole library — which viewers notice before the
creator does.

`AnthropicWriter` writes hooks that name what actually happened. It is off by
default: the suite runs offline, and a provider outage degrades the output
rather than failing the request. LLM hooks are scored by the same estimator as
template hooks, so the model's output is ranked rather than trusted.

Templates are English and are deliberately **not** translated for the other
four caption languages. Hook phrasing is idiomatic: a literal translation of
"Nobody talks about this" lands flat in German and reads as an accusation in
Dutch. The bank is keyed by language so adding one is data rather than code,
but each needs native authoring. An unsupported language falls back to English
and reports `language_supported: false` — wrong output is bad, silent wrong
output is worse.

| Module | Responsibility |
|---|---|
| `extraction.py` | Numbers, topic, outcome, timeframe, entity, quote. |
| `templates.py` | The bank — 89 patterns across ten types, plus the word-repeat guard. |
| `scoring.py` | Feature vector, weights, penalties, lift estimate, type affinity. |
| `llm.py` | Optional Claude-written hooks, scored by the same estimator. |
| `engine.py` | Slot fill, dedupe, type diversity, ranking. |
| `types.py` | `Hook`, `HookSet`, `CtrEstimate`, and the training-table contract. |

## The gameplay background engine

Speaker over a gameplay bed at **1080x1920, 60fps** — Subway Surfers, Minecraft
parkour, GTA driving, Rocket League, or satisfying loops.

```python
from clipforge.gameplay import Game, compose

plan = compose(duration_s=28.0, track=face_track, assets=library,
               game=Game.SUBWAY_SURFERS, word_count=78, speech=spans)
```

```
  output      1080x1920 @ 60fps  (28s)
  speaker     panel 1080x1152 at y=0    source crop 826x882
  gameplay    panel 1080x768 at y=1152  cover from 1518x1080
  camera      tracked   93 keyframes  1 cuts  94% held
```

The engine emits a **render plan** plus the ffmpeg filtergraph and camera
script that execute it. It decodes no frames and detects no faces: the speaker
track comes from an upstream detector, exactly as the caption engine takes
word-level timings rather than inventing them.

### The camera is the hard part

A face tracker gives you boxes that jitter several pixels between frames,
vanish whenever the speaker turns their head, and arrive at 10fps when the
output is 60. Cropping straight to them produces video that is unpleasant to
watch — none of which is visible looking at boxes on a chart, and all of which
is visible in the first second of playback.

| Mechanism | Without it |
|---|---|
| Deadband with hysteresis | The frame chases a head sway, and chatters on and off at the threshold. |
| Adaptive smoothing (1€ filter) | Fixed smoothing forces a choice between visible jitter when slow and visible lag when fast. |
| Exponential follower | The camera sprints at its speed ceiling and halts — the exact lurch the deadband exists to prevent. |
| Slew limit | One detector glitch throws the frame across the shot. |
| Cut, don't pan, past 42% of frame width | Panning to a second speaker is nauseating; a cut is invisible. |
| Minimum shot length | Two people in conversation strobe the frame on every "mm-hm". |
| Cut confirmation over 0.15s | One spurious box becomes a cut, and the minimum shot length then strands the camera on empty background for 1.2s. |
| Hold through detection gaps | Recentring during a two-frame dropout and coming back — the most obvious artefact an auto-framer can produce. |
| Eyeline at 40%, not box-centre | A visible slab of dead space above the head. Headroom is a composition rule, not a rounding error. |

**The crop size is constant for the whole clip.** Two independent reasons agree:
zooming within a shot is amateurish, and ffmpeg cannot change a filter's output
dimensions mid-stream, so a variable crop could not be executed anyway. The
camera pans; it never zooms. Size comes from the 90th percentile of observed
face height — the mean gives a crop that is correct on average and too tight
exactly when the speaker leans into the camera, which is the moment the clip
was chosen for.

With no track at all the crop is a centred static one and the plan says
`tracking: "static"`. That is what an editor does with no information, and it
never looks broken — but the caller can tell it apart from a solved path.

### Salience is the design axis, not texture

A gameplay bed exists to occupy the attention that would otherwise scroll, not
to compete with the speaker. When the bed is *more* interesting than the person
talking, the viewer finishes the clip having absorbed nothing.

So salience is matched **inversely** to speech density — fast talking gets a
quiet floor — and a high-salience bed is given *less* room, because it does not
need the space and every pixel comes out of the only panel carrying
information.

| Bed | Salience | Handling |
|---|---|---|
| Satisfying | 0.22 | Lowest salience; the correct default behind information-dense speech. |
| Minecraft Parkour | 0.40 | Centre-weighted forward motion; crops to 9:16 with little loss. |
| Subway Surfers | 0.55 | Natively vertical — keeps full width in the band, losing only a vertical slice. |
| GTA Driving | 0.72 | Wide horizontal action; **fitted**, not cropped, or the road is gone. |
| Rocket League | 0.85 | Highest salience and worst crop candidate. The ball crosses the full pitch; a fixed crop loses it, and a crop that chases it is a second moving camera fighting the first. |

Footage that cannot survive a crop is scaled whole into the band with a
blurred, cover-cropped copy of itself behind. Flat bars read as a mistake; a
blurred fill reads as a choice.

### Timing is three problems, none of them "make the durations equal"

**Frame rate.** Gameplay is usually 60 and survives untouched; the speaker is
usually 30 and is conformed by frame *duplication*. Motion interpolation is
available and is never the default — interpolating a talking head smears the
mouth on every plosive, far more visible than the judder it removes. The engine
distinguishes an exact ratio from a near one: 29.97 into 60 is 2.001x, about one
extra duplicated frame every 17 seconds. Invisible, but not clean, and it says
so rather than claiming "no judder".

**Loop seams.** A bed shorter than the clip repeats, and an arbitrary jump back
to zero is the clearest tell that a video was machine-assembled. The engine
cannot find visually continuous loop points without decoding frames, so it uses
declared ones and marks the seam `visible` when it cannot. Marking it is the
point.

**Where seams land.** A visible seam belongs *mid-sentence*, not in a pause.
Attention sits on whichever panel is doing something; during a pause it drifts
to the bed, which is exactly when a discontinuity gets noticed. Seams are
nudged **into** speech — the opposite of the intuitive placement.

Start offsets are chosen deterministically from a per-clip seed and steered
away from recently used ones. The same twenty seconds of Subway Surfers behind
a creator's entire library is noticed by their audience long before it is
noticed by them.

### The filtergraph

The camera path is executed by `sendcmd`, not by expressions — `crop` accepts
runtime commands for `x` and `y`, so a piecewise path becomes a timestamped
script. That is only practical because of the deadband: a 28-second clip is
1680 frames and about 90 keyframes.

```
[0:v]fps=60,sendcmd=f='camera.cmd',crop@spk=w=776:h=882:x=404:y=44,scale=1080:1228[spk];
[1:v]split=2[s0][s1];
[s0]trim=start=0.0000:duration=16.5000,setpts=PTS-STARTPTS[g0];
[s1]trim=start=0.0000:duration=11.5000,setpts=PTS-STARTPTS[g1];
[g0][g1]concat=n=2:v=1:a=0[gpsrc];
...
[spk][gp]vstack=inputs=2[v]
```

**These graphs are emitted, not executed, by the test suite** — there is no
ffmpeg in the test environment, and a test that shelled out to one would be an
integration test wearing a unit test's clothes. What `link_check` does verify is
the property that actually breaks graphs: every pad produced is consumed exactly
once, and every pad consumed is produced. That check earned itself immediately
by catching a looping graph that named `[1:v]` twice — legal-looking, and
rejected outright by ffmpeg, which needs an explicit `split`.

Gameplay audio is never mapped. It competes with the speech it is there to
support, and the music on most gameplay beds carries a claim of its own,
separate from the game's.

### Rights

Recorded gameplay is someone else's copyrighted audiovisual work, and the five
sources do not sit under one policy: Mojang's guidelines are permissive about
monetised Minecraft content, Psyonix/Epic publish a fan content policy, and
Take-Two has historically been the most aggressive rightsholder here. "Everyone
does it" describes enforcement patterns, not a licence. Ship a per-game rights
posture and a first-party or licensed asset library before this is a paid
feature. The engine records the asset id on every plan so an asset can be traced
and pulled.

| Module | Responsibility |
|---|---|
| `catalog.py` | Per-game salience, aspect, action placement, crop viability, rights posture. |
| `camera.py` | The virtual camera — 1€ filter, deadband, follower, slew limit, cuts, gap handling. |
| `layout.py` | 1080x1920 panel composition, split ratios, caption band, safe zones. |
| `timing.py` | fps conform, loop segmentation, seam placement, offset selection. |
| `render.py` | ffmpeg filtergraph, `sendcmd` camera script, argv, structural link check. |
| `engine.py` | Orchestration. |
| `types.py` | The render plan — deterministic and hashable, so it can be a cache key. |

## The publishing system

TikTok, YouTube and Instagram. OAuth connection, recurring schedules, bulk
imports, a content calendar, and a worker loop that retries — holding posts
weeks or months ahead.

```python
from clipforge.publish import PublishingSystem, weekdays_at

system = PublishingSystem(timezone="Europe/Amsterdam")
system.connect(account, tokens)
placed, rejected = system.schedule_bulk(
    "yt-main", specs, weekdays_at(17, 0, "Europe/Amsterdam")
)
```

### Read `automation_report()` before believing the word "automated"

The request was "complete automation". Two of the three platforms put
something in the way, and the system reports it when an account is connected
rather than discovering it at 6am three weeks later:

| Platform | What actually happens |
|---|---|
| **TikTok** | Direct Post needs TikTok's app audit. Until it clears, uploads land in the creator's **inbox as a draft** a human must finish, and visibility is forced to private. |
| **YouTube** | 10,000 quota units a day at 1,600 per upload is **six uploads a day per API project** — shared across every connected channel of every customer. Connecting more accounts does not raise it. |
| **Instagram** | Needs a Business or Creator account linked to a Facebook Page, and **pulls** the file from a public URL rather than accepting bytes. That URL must stay live through the whole transcode. |

A TikTok draft is not a published post, so it gets its own state
(`AWAITING_CREATOR`) rather than being counted as success. Putting a green tick
on a calendar next to something nobody can watch is the one lie a publishing
tool must not tell.

### "Months ahead" is a credentials problem, not a queue problem

Only YouTube offers real server-side scheduling (`status.publishAt`). For the
other two, "scheduled" means this system holds the job and fires it — which
puts every long-dated post at the mercy of a token surviving the wait.

| Platform | Credentials survive unattended for |
|---|---|
| YouTube | Indefinitely once the app is published — **but 7 days** while the consent screen is still in Testing, which is where most projects sit. |
| TikTok | 365 days (refresh token lifetime). |
| Instagram | **60 days.** The shortest, and the one that quietly kills long-dated schedules. |

So a six-month Instagram series does not get silently truncated — it gets
refused at schedule time, with the date and the reason:

```
⚠ SIX MONTHS ASKED FOR, 2 MONTHS BOOKED
  2026-10-31T08:30:00+00:00: instagram credentials for ig-main stop being
  renewable on 2026-10-31, before this post is due to run. It will fail
  unless the account is reconnected.
```

### Recurring schedules are stored in local time, and that is not pedantry

"Every weekday at 5pm" is a claim about wall-clock time. Storing it as a UTC
cron looks equivalent and is wrong twice a year — the whole schedule shifts by
an hour and nobody notices until a customer asks why their 5pm posts go out at
4pm. Rules carry an IANA zone and each occurrence converts individually.

Two transitions need explicit answers, and both are tested against real tzdata:

- **Spring forward.** 02:30 does not exist that day. Python constructs it
  happily and silently resolves it to 03:30, so a daily 02:30 slot posts an
  hour late exactly once a year. `NonexistentTime` chooses skip, shift, or
  next hour, and `dst_report()` names the affected occurrence before a customer
  commits to a quarter.
- **Fall back.** 01:30 happens twice. Naive expansion emits both and the same
  video goes out twice an hour apart. The default takes the first only.

A `31st` that does not exist in February is skipped, not rolled into 1 March —
posting on a day the customer did not choose is worse than not posting.

### Failure handling is five outcomes, not a backoff curve

**A retry that double-posts is worse than a post that failed.** A failure is a
notification; a duplicate is a creator's audience seeing the same video twice.
So the classifier's most important job is the ambiguous case.

| Disposition | When | Why not just retry |
|---|---|---|
| `RETRY` | 5xx, timeout *before* anything was sent | — |
| `RECONCILE` | timeout or 5xx **after** the platform was told to create something; any 409 | The post may already exist. Ask what the account has before sending anything. |
| `RESCHEDULE` | quota exhausted, 429 | Doubling 30s → 8m against a budget that resets at midnight burns the retry budget and then fails a post whose real answer was "tomorrow". |
| `REAUTH` | 401, dead refresh token | No amount of waiting reconnects an account. It needs a human. |
| `FAIL` | caption too long, banned account, unsupported format | Retrying cannot help. |

Backoff jitter is derived from the post's own idempotency key rather than drawn
randomly: a platform outage fails every queued post at once, and without jitter
they all retry in lockstep the moment it recovers. Deriving it keeps a replayed
queue reproducible.

Workers take a **lease**, not a lock, so a worker killed mid-upload frees its
job within the hour without two workers ever holding it at once.

### The calendar answers questions before the schedule runs

Conflict detection covers spacing (two posts close enough to split their own
audience), per-account daily caps, and — separately — YouTube's project-scoped
quota, which no per-account check would catch: four uploads to each of two
channels is eight against a six-a-day budget while neither channel exceeds its
own limit.

`capacity_forecast()` is what a bulk-upload button should show before it is
pressed:

```
CAPACITY FORECAST for 200 YouTube uploads
  6/day, project-scoped
  34 days — finishes 2026-10-04
  6 a day for the whole API project, shared across 2 account(s) — connecting
  more does not help
```

### Requests are built, never performed

Adapters are pure state machines over a `Transport`, so every branch of every
platform protocol is exercised offline with scripted responses — a publisher
whose test suite needs live credentials is a publisher whose test suite does
not run. It also keeps secrets out of the layer that formats logs:
`Request.redacted()` strips bearer tokens, client secrets and PKCE verifiers.

The three protocols share nothing. YouTube is a Google resumable upload where
`308` carries the authoritative byte count (trusting the local offset after a
partial write corrupts the resume). TikTok declares chunk size and count up
front, and the **last chunk absorbs the remainder** — an exact ceil-division
split leaves an undersized final chunk that TikTok rejects. Instagram creates a
container, polls it, and publishes with a second call.

**No live transport ships.** `RecordingTransport` replays scripted responses;
there is no HTTP client, no credential handling in production form, and the
endpoint shapes in `limits.py` are third-party facts stamped
`LIMITS_VERSION = "2026-08-verify-quarterly"` — re-check them before trusting
any number here.

Token storage is the other unfinished edge. `InMemoryTokenStore` is a
reference; `SealedTokenStore` takes `seal`/`unseal` callables rather than
implementing crypto, because refresh tokens are long-lived credentials to other
people's audiences and that key belongs in a KMS held by something other than
the process that publishes.

| Module | Responsibility |
|---|---|
| `limits.py` | Media, quota, token and automation facts per platform. One file, version-stamped, because they change. |
| `oauth.py` | PKCE flows, token exchange and refresh, credential horizons. |
| `schedule.py` | Recurrence in local time, with DST gaps and folds handled explicitly. |
| `calendar.py` | Occupancy, conflicts, free slots, capacity forecasting, month view. |
| `retry.py` | Failure classification into five dispositions; jittered backoff. |
| `adapters.py` | The three upload state machines, as request builders. |
| `engine.py` | Scheduling, validation, leases, and the worker loop. |

## The channel factory

Create a channel from a niche; the factory finds content, clips it, writes
hooks, builds captions, composes the frame and schedules the upload — seven of
them at once, independently.

```python
from clipforge.factory import ChannelFactory, Niche

factory = ChannelFactory(publisher=publishing_system, finder=registry)
cars = factory.create_channel("Redline", Niche.CARS, accounts={...})
factory.activate(cars.channel_id)
reports = factory.run_cycle()
```

```
Runway (Business)   1 scheduled, 0 blocked, 0 failed   191c
    ✓ src-business
        virality 55  24s  12 cues  split
        hook  “I lost fourteen million learning this”
              authority, predicted 6.5%
        → 3 post(s) queued
```

### A niche is a configuration, not a label

The seven differ in ways that conflict, and getting one wrong is visible in the
finished video:

| Niche | Bed | Captions | Clip | Cadence |
|---|---|---|---|---|
| Cars | **none** | punch | 15–30s | 2/day |
| Luxury | **none** | minimal | 15–25s | 2/day |
| Motivation | Subway Surfers | punch | 20–35s | 3/day |
| Business | Satisfying | karaoke | 25–45s | 2/day |
| Gaming | **none** | bounce | 15–30s | 4/day |
| AI | Satisfying | karaoke | 25–45s | 2/day |
| History | Minecraft parkour | typewriter | 30–50s | 1/day |

**Three niches get no gameplay bed at all.** The split-screen format exists to
give the eye something to do while someone talks. Cars, Luxury and Gaming
footage *is* the visual — Subway Surfers under a Lamborghini clip does not add
retention, it competes with the only thing worth looking at. Business and AI
get the lowest-salience bed available, because dense speech punishes a busy one.

Gaming is also the one niche routed to the **stream clipper** rather than the
viral engine: chat spikes find those moments and transcripts do not.

### The viral detectors don't cover every niche, and the factory says so

Running this end to end surfaced a real gap. The viral engine's detectors were
tuned on founder and podcast material, and a Cars or History clip can be the
best thirty seconds in an hour while registering **zero signal hits** —
"horsepower" and "besieged" are not in a taxonomy built around funding rounds.

Widening the general detectors would make every niche noisier to fix three, so
each niche carries its own vocabulary instead. That drives two things:

- **Re-ranking.** The general engine decides what is a strong moment; the niche
  decides which strong moment belongs on *this* channel, bounded so the base
  score still dominates.
- **A fallback**, when the general detector returns nothing at all. Windows are
  anchored on the utterance carrying the vocabulary, not swept at a fixed
  stride — a strided window happily returns one whose domain words are all in
  its last sentence, and the hook then writes itself about "a longer
  conversation" because that is what the text it was handed is about. Anything
  chosen this way is marked in the item's history, because it had no general
  signal behind it.

### Rights are a state gate, not a disclaimer

This is the part of "finds content" with no technical difficulty and a large
legal one. Clipping and reuploading someone else's video is infringement unless
something makes it lawful, and a factory running seven channels unattended does
it thousands of times before anyone looks. At that volume the exposure is not a
takedown — it is a pattern of commercial infringement across a portfolio.

So a source carries the basis on which it may be used, an item cannot leave the
`CLEARED` stage without one the channel accepts, and the default for anything
discovered rather than supplied is `UNVERIFIED`, which publishes nowhere.
Accepting unverified material is a named decision that appears in
`rights_report()`.

Every branch that blocks is a case that would otherwise become a takedown or a
licence breach:

| Blocked | Why |
|---|---|
| No basis recorded | The default, and the only safe one. |
| Licence expired | A schedule reaching past the expiry publishes without one. |
| No derivatives permitted | Clipping *is* a derivative work. |
| CC-NC on a monetised channel | The licence excludes exactly this use. |
| CC or stock with no attribution | The licence is void without it — and attribution is carried into the caption automatically. |

`expiring_soon()` exists because a factory booking a quarter ahead will publish
under a licence that lapses next month unless something checks.

`RegistrySourceFinder` serves sources an operator registered and cleared by
hand. That is not a placeholder: a rights-cleared pipeline genuinely looks like
this — a licence is signed, the source is entered, the factory works from the
registry. **An automated crawler is a different product with a different risk
profile and is deliberately not here.**

### Independence is enforced where it can be, and reported where it cannot

Channels hold their own budgets, breakers, queues and used-source sets, and
`run_cycle` catches everything a channel can throw. A channel with revoked
accounts and a channel with no budget both degrade to zero output and say why:

```
Redline      ran      1 scheduled
Momentum     skipped  no publishing accounts connected
Antiquity    ran      0 scheduled — budget: 50c left, item needs ~191c
```

Two details that matter more than they look. A **blocked** item — rights,
quality floor, budget — does not count against the circuit breaker, or one
unlicensed source would take a healthy channel offline; only errors do. And the
budget is checked *before* an item starts, because a pipeline that dies at the
render stage has already paid for transcription and detection.

**The one thing channels cannot have to themselves is YouTube's quota.** It is
per API project — six uploads a day for the entire factory. Seven channels
asking for two each is fourteen against six, and no amount of process isolation
changes that arithmetic:

```
⚠ youtube: the factory wants 14 posts a day against a 6/day project-scoped
  cap. 8 will not run. Raise the quota, cut cadence, or run fewer channels —
  adding accounts does not help when the scope is the project.
```

Allocation is **max-min fair**: equal shares, with whatever a channel does not
want flowing back to the ones that do, so a channel asking for one post a day is
not punished for sharing a factory with a channel asking for four. The
alternative — letting channels race and having the losers fail at post time — is
what happens by default, and it looks like a flaky product rather than a
capacity decision.

### What is not built

The pipeline runs on the six engines in this repository, so detection, hooks,
captions, composition and scheduling all execute for real offline. Two stages
are protocols with refusing defaults: `Transcriber` (there is no speech
recognition here, and `NullTranscriber` raises rather than inventing word
timings that would visibly drift) and the renderer (the gameplay engine emits a
filtergraph; nothing executes it). Per-stage costs are estimates for budget
control, not measurements.

| Module | Responsibility |
|---|---|
| `niches.py` | The seven profiles — signals, hook types, captions, bed, length, cadence, domain vocabulary. |
| `sources.py` | Discovery, provenance, and the rights clearance gate. |
| `channel.py` | Identity, accounts, budget, circuit breaker. |
| `pipeline.py` | The per-item stage machine over all six engines. |
| `scheduler.py` | Max-min fair allocation of shared platform quota. |
| `factory.py` | Orchestration and per-channel isolation. |

## The analytics intelligence engine

Tracks views, retention, likes, comments, shares and subscribers. Answers the
five questions — best posting times, hooks, topics, clip lengths, creators —
and writes weekly reports on a schedule.

```python
from clipforge.analytics import AnalyticsEngine

engine = AnalyticsEngine()
engine.track(record)
engine.ingest(source)
print(engine.report(week_end).render())
```

This is the engine the other six were built for. The viral ranker, the hook
estimator and the factory have each been persisting a feature vector and a
weights version with every decision so that outcomes could later be joined back
to them.

### Its failure mode is confidence, not error

Rank seven posting hours by mean views, bold the top one, and a creator
reorganises their week around three posts of noise. Four mechanisms stop that,
and all four are needed:

| Mechanism | Without it |
|---|---|
| Minimum sample (8 per group) | Three posts get a rank instead of "not enough data". |
| Permutation test | Normality assumed on view counts, which are nowhere near normal. |
| Benjamini-Hochberg FDR | 24 posting hours at p<0.05 yields a "winner" by chance, and the ranking guarantees it lands on top. |
| **Minimum effect size** | The mirror-image failure: with a tight spread a 3% change is statistically undeniable and operationally meaningless. |

That last one caught a real bug during the build. A synthetic *flat* week was
being reported as a significant −3% decline in views — genuinely significant,
completely worthless. A finding now has to clear both bars: distinguishable
from chance **and** big enough that acting on it could matter.

And when nothing clears them, `minimum_detectable_effect` turns "no significant
difference" from a dead end into a decision:

```
· Which topics reach furthest?
  No difference this data can resolve. Detecting a 50% effect would need
  about 108 posts per group; the largest group has 21.
```

The permutation test is validated against its own null: p-values are uniform
and the false-positive rate lands within a point of nominal at both 5% and 10%.

### Checked against known data

The demo plants exactly two effects and makes everything else noise, then
checks the engine on both counts:

```
✓ What hour of day performs best?              → 20:00 (+62%)
· Which topics reach furthest?                 no claim
· Whose source material performs best?         no claim
   ... 8 more: no claim

1 claim from 11 families of comparison over 146 posts.
```

The near-miss is the interesting one. The second planted effect — a +45%
creator — is deliberately set right at the detection floor. It ranks first at
+41% and the engine *still* refuses to call it, reporting instead that it would
need 108 posts per group. A ranking always has a top row; printing that row as
a finding is the whole failure this module exists to prevent.

A three-week-old account gets zero findings and eleven sample-size
requirements, which is the correct output.

### "Best hook" is unanswerable without deliberately publishing worse hooks

The factory publishes the top-ranked hook. Every outcome ever observed is
therefore an outcome for a hook the model already liked, so an analysis of that
data measures the model's preferences and will confirm the prior whatever the
prior was. It is the same trap the hook engine warned about: *a model trained
only on hooks that shipped learns which hooks get chosen, not which hooks work.*

`ExplorationPolicy` pays the cost — roughly one clip in seven publishes a hook
ranked 2nd to 5th instead of 1st — and in exchange the other six become
interpretable. `assess()` labels every comparison, and it distinguishes two
different problems rather than issuing one blanket disclaimer:

- **Confounded** — hook type, which the model chose. Needs exploration.
- **Observational** — posting time, clip length, creator. Not randomised, but
  not corrupted by the selection loop either.

Deliberately not a bandit: a bandit allocates on observed performance, which
reintroduces exactly the confounding and makes the causal question unanswerable
again. Fixed-rate randomisation is less efficient and gives an answer you can
trust.

### Retention is the only metric close to a cause

Views are downstream of distribution, distribution is downstream of retention.
But the *average* watch percentage collapses the useful part — a clip losing
40% in the first two seconds and one drifting off evenly produce the same
average and need opposite fixes.

```
past the hook   63%
of those, lost  50%
reach the end   33%

payoff: hooks are working — 63% get past them — but 50% of those who do
leave before the end. Shorter clips or a faster payoff, not better hooks.
```

Getting that denominator right was a second bug found during the build. Mid-clip
drop was measured against *all* viewers, so 63% → 33% read as a mild 31-point
decline and the engine called it "no dominant failure point". Measured against
the people who actually got past the hook it is half of them leaving — the same
error as reporting checkout conversion against total site traffic.

### Three ways to make numbers comparable

**Matched age.** A post published two hours ago has fewer views than one from
two weeks ago, and that says nothing about either. Metrics are stored as
append-only snapshots at fixed checkpoints, and a post too young for a
checkpoint is *excluded* rather than substituted — including it is how "recent
posts are underperforming" gets reported when they are merely recent. A single
mutable `views` column makes this reconstruction impossible afterwards.

**Per-platform baselines**, learned from the account's own history as a median
once there are twelve posts, so one viral clip cannot redefine normal.

**Trimmed means**, because view distributions are heavy-tailed enough that a
plain mean of ten posts is mostly a report on whether one of them went viral.

### Do the priors actually predict anything?

```
hook CTR estimator  (hook-heuristic-v1)
  n=146  no better than chance (rho -0.04). The hand-tuned weights are not
  carrying information; retrain on the feature rows rather than tuning them.
```

This is what `predicted_lift` and the weights versions were persisted for.
`calibration()` distinguishes three outcomes — predictive, useless, and
*inverted*, where the model's preferred clips do systematically worse, which is
the one worth an alarm because the weights carry real signal with the wrong
sign.

### What is not built

No live metric collection. The three platforms expose different reporting APIs
at different granularities on different delays — only YouTube supplies a real
retention curve, which is why 80 of 146 posts in the demo have one and the
absence is visible rather than imputed. `RecordedSource` replays snapshots; a
stub pretending otherwise would produce analyses whose limits only appeared in
production.

| Module | Responsibility |
|---|---|
| `metrics.py` | Snapshots, retention curves, matched-age lookup, per-platform baselines. |
| `stats.py` | Permutation tests, bootstrap CIs, FDR control, minimum detectable effect. |
| `attribution.py` | Joining outcomes to the decisions that produced them. |
| `experiments.py` | Exploration policy and causal-validity assessment. |
| `insights.py` | The five questions, plus retention diagnosis and model calibration. |
| `report.py` | Weekly reports, with week-on-week deltas that can say "flat". |
| `engine.py` | Ingest, scheduling, readiness. |

## Empire Mode

Fifty-plus channels, multiple brands, multiple users, one scoped dashboard.

```python
from clipforge.empire import Empire, Plan, Role

empire = Empire(factory, analytics)
tenant = empire.add_tenant("Northwind Media", Plan.EMPIRE)
brand = empire.add_brand(tenant.tenant_id, "Redline")
print(empire.dashboard(user.user_id).render())
```

This is the first layer that stresses the system rather than extending it, and
running it at scale produced three findings that are arithmetic rather than
opinion.

### Scheduling was O(n²), measured and fixed

Every `schedule()` scanned the whole calendar to check spacing:

| Posts | Before | After |
|---|---|---|
| 300 | 8ms | 4ms |
| 3,000 | 469ms | 35ms |
| 45,000 | ~105s (extrapolated) | **0.95s** |

A per-account index with a bisect lookup made the per-post cost flat at
0.012ms instead of climbing linearly. `TestScale` pins it, because a quadratic
insert is fast in every unit test and slow only in production.

### 500 uploads/day is reachable, but not evenly

```
PLATFORM     ACCOUNTS   CAP     SCOPE   CEILING
tiktok             52     6   account       312
youtube            46     6   project         6
instagram          33    25   account       825
```

TikTok and Instagram caps are **per account**, so they scale with channels.
YouTube's is per **API project** — six a day for the whole app, whether one
channel is connected or fifty. An empire at this volume is a TikTok operation
with a YouTube trickle.

Getting YouTube to an even 167/day needs 28 API projects. There are two ways
to have those and only one is legitimate: **per-tenant projects**, where each
customer connects their own Google Cloud project and spends their own quota on
their own content. One operator standing up 28 projects to multiply their own
allowance is quota circumvention, and they are terminated together — every
channel stops on the same afternoon. `QuotaPool.ownership` models the
difference and `circumvention_risk()` flags the shape.

### Ad revenue does not pay for this, and the dashboard says so

The cost is already in the repo: `ITEM_COST_CENTS` is 191c per clip. The
revenue side needs three facts, and the second surprises people:

- YouTube Shorts pays roughly $0.02–0.07 per thousand views.
- **TikTok's Creator Rewards only pays on videos over one minute.** Every clip
  this system makes is 15–60s, so it earns nothing. Not a low RPM — zero.
- Instagram has no general Reels revenue share.

Run 500/day for a month at the forced platform mix:

```
15,000 uploads → 45,000,000 views

  ad revenue          $22
  production cost     $28,650
  net (ads only)      $-28,628

blended RPM      0.0480c per 1,000 views
break-even       3,979,167 views per clip
actual           3,000
short by         1,326x
```

Forty-five million views generating twenty-two dollars. That is not an
argument against the product — it is the reason the revenue line has to be
sponsorship, affiliate, lead generation or the subscription itself, and the
reason a dashboard reporting ad revenue as "total revenue" is selling a
fantasy. `required_non_ad_revenue_cents()` states the number a business plan
should start from.

### Four totals, each with its distribution attached

```
uploads                  367        channels          52
views              1,524,334        brands             4
subscribers            4,716        shares         9,398
revenue              $21,555        net          $20,854

top channel    18% of views    top 10%    49%    dormant 0
```

A total hides the distribution, and at fifty channels the distribution is the
story — "1.2M views" is the same number whether every channel contributed or
one clip went viral. Growth is reported twice for the same reason: raw, and
**same-channel**, because a portfolio that grew from 40 to 50 channels shows
25% growth from arithmetic alone. Both go through the analytics engine's
significance machinery, so a flat week reads as flat.

### Alerts come before totals

At fifty channels a dead one moves the portfolio total by 2%, which is
indistinguishable from a slow week. So the dashboard leads with the channels
that stopped, ran out of budget, lost credentials, or hold a licence expiring
inside the scheduling horizon — each with what to do about it.

### Isolation is a query concern

Every lookup takes a tenant and filters on it; `require()` raises rather than
returning a boolean, so the call site that forgets fails closed. Roles are
ordered by blast radius rather than seniority — an operator can pause a channel
but not disconnect an account, an editor schedules but cannot see revenue, and
an agency's client sees one brand:

```
owner@northwind.test    owner    sees 52 channels
editor@northwind.test   editor   sees 52 channels
client@redline.test     viewer   sees 13 channels

✗ analyst@northwind.test is analyst and cannot manage channels
✗ editor@northwind.test is editor and cannot view revenue
```

### One more bug worth naming

`round(float("inf"))` raises `OverflowError`, and break-even views is
legitimately infinite whenever the blended RPM is zero — which is the *normal*
case for a TikTok-and-Instagram portfolio. The dashboard's JSON endpoint would
have crashed for most accounts. It now serialises as `null` with
`break_even_unreachable: true`, which is the honest encoding.

| Module | Responsibility |
|---|---|
| `tenancy.py` | Tenants, brands, users, roles, plan limits, isolation. |
| `capacity.py` | Platform ceilings, quota pools, circumvention detection. |
| `economics.py` | Revenue, cost, blended RPM, break-even. |
| `rollup.py` | Totals, concentration, same-channel growth, leaderboard. |
| `dashboard.py` | The single scoped view, alerts first. |
| `empire.py` | Orchestration over factory, publisher and analytics. |

## Persistence

Everything above decides. This is where the decisions are kept.

PostgreSQL 16, schema and migrations owned by Prisma, repositories in
`src/clipforge/store/`. Full detail — roles, migration workflow, the
conventions a new model has to follow — is in [`db/README.md`](db/README.md).

```python
from clipforge.store import open_database

db = open_database()                     # DATABASE_URL, or in-memory
with db.unit_of_work("ten_acme") as uow:
    uow.channels.save(channel)
    uow.jobs.enqueue(job)
# committed on the way out; rolled back if the block raised
```

Ten entity types persist: users, channels, projects, videos, sources, clips,
schedules, uploads, metrics and jobs — plus tenants, connected accounts,
operator-entered revenue and API quota pools.

### Prisma without the Prisma client

Prisma is two halves. The client is a TypeScript library; this is a Python
project, so there is no `generator` block and nothing is generated. The
migration engine is what is used: one declarative schema, a checksummed
migration history, and plain SQL on disk that a reviewer reads before it
reaches production. `prisma-client-py` is a third-party port rather than a
Prisma product, and it is not what to bet a data layer on.

### The stores that used to be dictionaries

The engines take their storage as an argument. Passing nothing gives the
in-memory version — what the demos and most tests use, and what loses
everything when the process ends. Passing the durable version gives the same
engine, backed by Postgres, driven identically.

| Was | Now | Table |
|---|---|---|
| `InMemoryTokenStore` | `DurableTokenStore` | `social_accounts` (sealed) |
| `PublishingSystem.accounts` | `DurableAccountBook` | `social_accounts` |
| `ContentCalendar` | `PersistentCalendar` | `uploads` |
| `RegistrySourceFinder` | `DurableSourceRegistry` | `sources` |
| `ChannelFactory.channels` | `DurableChannelBook` | `channels` |
| `AnalyticsStore` | `DurableAnalyticsStore` | `metric_snapshots` |
| `PublishingSystem.series` | `DurableSeriesBook` | `schedules` |
| `empire.Directory` | `DurableDirectory` | `tenants`, `projects`, `users` |
| `Empire.revenue` | `DurableRevenueBook` | `revenue_entries` |
| `Empire.pools` | `DurablePoolList` | `quota_pools` |

```python
from clipforge.store import open_database
from clipforge.store.durable import (
    DurableAccountBook, DurableTokenStore, PersistentCalendar,
)

db = open_database()
system = PublishingSystem(
    token_store=DurableTokenStore(db, tenant, seal=kms.seal, unseal=kms.unseal),
    accounts=DurableAccountBook(db, tenant, channel_id=channel_id),
    calendar=PersistentCalendar.restore(db, tenant, channel_id=channel_id),
)
```

Writes go through before the call returns. Batching them would be faster and
would reintroduce exactly the failure being removed: work that looked saved and
was not.

The calendar is the one exception to being a pass-through. It keeps its sorted
in-memory index, because that is what makes the spacing check a binary search
instead of a query per insert, and Postgres holds the truth. That index is a
cache of a **window** — `load()` takes a horizon, because 500 uploads a day
ninety days out is 45,000 rows and a year of them is not a working set. Posts
beyond the horizon are in the table and still publish; they are simply not in
this process's view until the window moves.

### Tenant isolation

Every tenant-scoped table has row-level security enabled *and* forced, with
`tenant_id = app.current_tenant()` as both the read predicate and the write
check. The tenant is set per transaction with `SET LOCAL`, which is what makes
it safe behind a connection pool: it dies with the transaction, so a pooled
connection cannot carry one customer's tenant into the next customer's query.

The application connects as `clipforge_app`, which is neither superuser nor the
table owner — Postgres lets both bypass row-level security, so an application
connected as either turns every policy into a no-op while the isolation tests
keep passing.

`app.current_tenant()` raises when the scope is unset rather than returning
NULL. NULL would fail every policy closed, which sounds safer and is not: a
forgotten scope then looks like a tenant with no data, and "the dashboard is
empty" gets triaged as a product bug for a week.

Two guarantees are database privileges rather than conventions. The app role
holds no UPDATE or DELETE on `metric_snapshots`, so the append-only rule every
analytics finding depends on cannot be broken by a future repository method.
And `FORCE` covers the owner, so an application pointed at the migration role
by a copy-pasted `DATABASE_URL` fails on its first query instead of quietly
serving every tenant's rows to everyone.

### Testing it

```bash
PYTHONPATH=src python -m unittest discover -s tests -t tests   # in-memory; Postgres cases skip

createdb clipforge_test -O clipforge_owner
(cd db && DATABASE_URL=postgresql://clipforge_owner:...@localhost/clipforge_test \
   npx prisma migrate deploy)
CLIPFORGE_TEST_DSN=postgresql://clipforge_app:...@localhost/clipforge_test \
CLIPFORGE_TEST_ADMIN_DSN=postgresql://clipforge_owner:...@localhost/clipforge_test \
PYTHONPATH=src python -m unittest discover -s tests
```

- `test_store_contract.py` — one suite, run against both implementations, so
  the fast in-memory tests are evidence about the Postgres path rather than
  about themselves.
- `test_row_level_security.py` — raw SQL with no WHERE clause, as the app role.
  The contract tests would pass with RLS switched off, since the repositories
  also check tenancy in Python; these would not.
- `test_restart_survival.py` — the writer is a child process **SIGKILLed** after
  it commits, so whatever the reader finds was in Postgres already. Also asserts
  the other half: a process killed *before* its commit leaves nothing behind.
- `test_durable_publishing.py`, `test_durable_factory.py` — the engines,
  restarted.

| Module | Responsibility |
|---|---|
| `records.py` | One dataclass per table. |
| `schema.py` | Table descriptors; column lists derived, never restated. |
| `protocols.py` | Repository and unit-of-work interfaces. |
| `memory.py` | In-memory implementation. A test double, not a deployment. |
| `postgres.py` | psycopg implementation, pooled. |
| `mappers.py` | Domain objects to rows, losslessly, and back. |
| `durable.py` | Drop-in durable replacements for the engines' dictionaries. |

`DurableDirectory` is scoped to one tenant, where the in-memory one held every
tenant at once. That is a real change in what the object can answer and the
right one: the application role connects under row-level security and cannot
read across the boundary. A cross-tenant listing is a control-plane operation —
billing, support, provisioning — and belongs to a role allowed to see across
tenants, in the way `clipforge_worker` is for the queue. Giving the request path
that reach is how one customer's dashboard ends up counting another's channels.

Product rules stay in the base classes throughout: source ranking, the
analytics statistics, plan limits, and the permission model (which raises
rather than returning a bool, so a forgotten branch fails closed). Two
implementations of the same rule drift apart, and the one nobody reads is the
one that ships.

## Source acquisition

Where material comes from. Four inputs behind one resolver, one queue and one
persistence path.

```python
from clipforge.acquire import AcquisitionEngine, AcquisitionConfig

engine = AcquisitionEngine(db, "ten_acme",
                           config=AcquisitionConfig(workspace="/var/lib/clipforge/media"))
engine.submit("https://youtu.be/dQw4w9WgXcQ")        # one video
engine.submit("https://www.youtube.com/@studionine") # a channel, expanded
engine.submit("https://feeds.example.com/show.xml")  # a feed, expanded
engine.submit("/uploads/interview.mp4")              # a file
engine.run(limit=10)                                 # a worker turn
```

`submit` resolves and queues; `run` drains. They are separate because
acquisition is slow and the caller submitting is usually a web request that
must not wait for a two-gigabyte podcast.

### Normalisation is the point of the resolver

The same YouTube video has at least six URL forms. A system that treats them
as different videos downloads it six times and posts the same clip to the same
channel six times, so `resolve` reduces all of them to the bare id — and
strips tracking parameters, notes a playlist id without following it, and
refuses a path that does not exist rather than treating it as a URL.

Feed items are keyed on their **GUID**, not their enclosure URL. Enclosure
URLs change whenever a podcast host moves CDN or rewrites a tracking prefix,
and deduplicating on URL re-downloads an entire back catalogue when they do.

### Resumable downloads

`http.py` speaks `Range` and needs three things to resume safely, all of which
it keeps:

1. **Bytes on disk** — in `<path>.part`, whose length *is* the resume offset. A
   counter kept anywhere else disagrees with the file after a crash.
2. **A validator** — `If-Range` with the original `ETag` or `Last-Modified`, so
   a file that changed on the server restarts instead of splicing the tail of a
   new encode onto the head of an old one. That splice is exactly the right
   size, passes every size check, and fails to decode hours later in the
   renderer.
3. **Proof the server honoured it** — a `206`. A server that ignores `Range`
   and answers `200` gets the partial file truncated, because appending a whole
   file to a partial one yields the first bytes twice.

Retries are classified by what a caller should do, not by where the error came
from: timeouts, resets and 5xx back off with full jitter; 4xx do not, except
408, 425 and 429, which are the server asking for patience. Retrying a 404
eight times is a queue spending its afternoon on a video deleted last week.

### Measuring media without ffprobe

`mp4.py` is an ISO base media file format reader — duration, geometry, codecs,
cover art — straight from the container boxes. It is used before ffmpeg for
three reasons: it answers on a **partially downloaded** file, so "is this worth
finishing?" is answerable before the bandwidth is spent; it cannot be absent,
where ffprobe ships separately and plenty of static ffmpeg builds omit it; and
`mvhd` carries duration and timescale as integers, so the answer is exact
rather than rounded through a text formatter.

ffmpeg handles what the box reader does not — WebM, Matroska, MP3 — and
cross-checks it where both can answer. When the two disagree by more than half
a second the ffmpeg number wins and the disagreement is recorded, because a
container header that lies is how a clip gets cut past the real end.

A duration that could not be measured is `None`, never `0`. Zero is a number
the clip detector divides by.

### Thumbnails, cheapest origin first

1. **Embedded** — cover art already in the container. A read, no decoding, and
   how a podcast episode gets artwork on a machine with no ffmpeg.
2. **Remote** — the platform's own thumbnail, already written by yt-dlp.
3. **Frame grab** — ffmpeg seeks in and writes one frame.

Only the third needs ffmpeg, so thumbnail extraction degrades to "no frame
grabs" rather than "no thumbnails". A grab does not take frame zero: the first
frame of a talking-head video is usually a black fade-in, and a wall of black
thumbnails looks like a broken product. When none of the three work the result
is `None` — never a placeholder, which is indistinguishable downstream from a
real thumbnail until a hundred of them reach a feed.

### Acquisition does not grant rights

Everything this layer creates is `RightsBasis.UNVERIFIED`, which the channel
gate refuses by default. Downloading something and being allowed to republish
it are different questions, and a layer that answered both would turn a
licensing decision into a technical one. A re-crawl never resets a basis an
operator has recorded — that would silently take a cleared show off the air.

The single exception is `mark_owned`, for a customer's own uploads: the
customer supplying the footage is the rights holder.

**YouTube's Terms of Service prohibit downloading except through features
YouTube provides.** Material a customer owns, material under a Creative
Commons licence, and material a creator has given written permission for are
the defensible cases; a general crawler over other people's uploads is not,
whatever the code is capable of. `AcquiringSourceFinder.clearable()` is the
operator's work queue of material waiting on that decision, which is why a
channel wired to it can look busy and publish nothing.

### Replacing hand-entry

`RegistrySourceFinder` — an operator entering cleared sources by hand — stays,
because that is what a rights-cleared pipeline genuinely looks like. What it
cannot do is *get* anything. `AcquiringSourceFinder` watches a set of inputs,
sweeps them, and answers the factory's `find` from what actually landed:

```python
finder = AcquiringSourceFinder(db, tenant, engine)
finder.watch("https://feeds.example.com/show.xml", topics=("business",))
finder.watch("/uploads/", owned=True)
finder.sweep()                       # submit, drain, stamp topics
factory = ChannelFactory(finder=finder, channels=DurableChannelBook(...))
```

`find` never blocks on acquisition; `sweep` is a separate call a scheduler
makes. A cycle that waited for a two-gigabyte podcast before deciding what to
clip would be a cycle that times out.

### What is persisted

`sources` is the library — material a channel may clip. `acquisition_runs` is
the record of the *work*: what was asked for, how far it got, what it turned
out to be, what went wrong. Keeping them apart is what stops a half-downloaded
file appearing as something to clip, and stops a failure vanishing.

| Module | Responsibility |
|---|---|
| `resolve.py` | What did the operator paste? Normalisation and dedupe keys. |
| `http.py` | Resumable downloads; retry classification and backoff. |
| `mp4.py` | ISO container reader: duration, geometry, codecs, cover art. |
| `probe.py` | Measuring and thumbnails; ffmpeg where boxes cannot answer. |
| `rss.py` | Podcast feeds, RSS 2.0 + iTunes + Atom, standard library only. |
| `youtube.py` | yt-dlp: metadata, downloads, channel listings. |
| `engine.py` | Resolve, queue, download, probe, thumbnail, persist. |
| `finder.py` | The `SourceFinder` that acquires instead of waiting. |

### Running the tests

The download, feed, probe and thumbnail paths are tested against **real
bytes**: a real HTTP server on a real socket serving real MP4 files, with the
server made to misbehave on purpose — dropping connections mid-body, ignoring
`Range`, changing its `ETag` between attempts. Those are the cases that corrupt
files and they are hard to provoke against a well-behaved origin.

Fixtures are generated with ffmpeg:

```sh
mkdir -p /tmp/mp4test && cd /tmp/mp4test
ffmpeg -f lavfi -i "testsrc=size=640x360:rate=25:duration=7" \
       -f lavfi -i "sine=frequency=440:duration=7" \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest \
       -movflags +faststart real.mp4
ffmpeg -f lavfi -i "sine=frequency=220:duration=12" -c:a aac audio.m4a
ffmpeg -f lavfi -i "color=c=red:size=320x320:duration=1" -frames:v 1 cover.png
ffmpeg -i audio.m4a -i cover.png -map 0:a -map 1:v -c:a copy -c:v mjpeg \
       -disposition:v attached_pic podcast.m4a

CLIPFORGE_FFMPEG=$(command -v ffmpeg) PYTHONPATH=src \
  python -m unittest discover -s tests
```

Without the fixtures those cases skip and say so.

**The YouTube adapter's network leg is not covered by tests.** It is driven
through recorded `info_dict` payloads, which exercises the mapping, the error
classification and the channel walk — where this adapter's own bugs live — but
not that yt-dlp can reach YouTube.

## Rendering

The gameplay engine decides the composition and builds an ffmpeg filtergraph.
This is the part that runs it.

```python
from clipforge.render import RenderEngine, RenderConfig

engine = RenderEngine(db, "ten_acme",
                      config=RenderConfig(workspace="/var/lib/clipforge/renders"))
engine.enqueue(clip_id, plan, speaker_path,
               gameplay_path=bed, subtitles=to_ass(track, style))
engine.run(limit=2)
```

Output is 1080x1920 at 60fps, H.264 high profile, CRF 18, faststart, with AAC
audio at 192k — and the demo renders one at roughly realtime on a single core.

### ffmpeg exiting zero is not a successful render

It exits zero on plenty of files that are not what was asked for: a truncated
encode, a stream silently dropped because a `-map` matched nothing, a height
rounded odd by a `scale`. Every one passes a "did the process succeed?" check
and none is publishable. So the output is probed and compared against the plan
before it counts.

The comparison is tolerant about duration — encoders land a frame either side
and failing over 16ms would fail most correct renders — and exact about
geometry, frame rate and the presence of audio. **A missing audio track is the
one that matters most:** `-map 0:a?` is optional by design so a broken audio
chain does not fail the render, which means the failure mode is a perfectly
valid silent video that nobody notices until it is on someone's feed.

### The camera crop is in source pixels

`SpeakerTrack` defaults to 1920x1080, so a plan composed without a real track
and applied to a 1280x720 file asks ffmpeg to crop a 1080-pixel-tall window
out of a 720-pixel-tall frame. ffmpeg's answer is:

```
Invalid too big or non positive size for width '996' or height '1080'
Error reinitializing filters!
Conversion failed!
```

which names neither the plan, the source, nor the mismatch. A pre-flight probe
turns that into a sentence saying what to fix, and marks it permanent — the
same plan against the same file fails identically for ever.

### Captions are burned, not muxed

TikTok, Reels and Shorts all play with soft subtitles off by default, so a soft
track is a caption nobody sees — and captions are load-bearing for retention on
muted autoplay. The ASS file the caption engine already exports is composited
over the finished frame, after the camera and panel chains, so burning cannot
disturb the composition.

### Atomic outputs

ffmpeg writes to `<output>.tmp.mp4`, and the file is renamed into place only
after it has been measured. A worker killed mid-encode leaves a `.tmp` nobody
reads rather than a short file the publisher uploads.

### Testing it

Every render test spawns real ffmpeg and measures the result with the container
reader from `acquire.mp4` — the filtergraph is built by string concatenation
and the only way to know a graph is valid is to hand it to ffmpeg.

Clips are short (1.2s) and encoded at `ultrafast`, keeping the suite around
thirteen seconds. The geometry is the real 1080x1920 60fps throughout: shrinking
the canvas would desync the camera path, whose coordinates are in source pixels.
`CLIPFORGE_SLOW_TESTS=1` adds a render at the shipping `medium`/CRF 18 settings.

```sh
CLIPFORGE_FFMPEG=$(command -v ffmpeg) PYTHONPATH=src \
  python -m unittest tests.test_render_engine
```

Without ffmpeg the render tests skip and say so.

| Module | Responsibility |
|---|---|
| `types.py` | Requests, results, and errors split by what a caller should do. |
| `engine.py` | Execute, verify, persist; the queue, retries and pre-flight. |

## Transcription

Media in, words with timings out. The stage everything downstream depends on:
the detection engine reads a transcript, the caption engine reads word-level
timings, and neither has anything to do without this.

```python
from clipforge.transcribe import TranscriptionEngine, provider_from_env

engine = TranscriptionEngine(db, "ten_acme", provider_from_env())
engine.enqueue(source_id, media_path)
engine.run(limit=2)
transcript = engine.transcript_for(source_id)
```

Every provider returns the same `Transcript`: full text, segments, word-level
timings, per-word confidence where the provider reports it, and the detected
language with its own confidence. `confidence` is `None` rather than `1.0`
when a provider does not report one — a fabricated certainty is worse than an
absent one, because something downstream will filter on it.

```sh
python demo/run_transcribe_demo.py --check          # what is configured
python demo/run_transcribe_demo.py --input talk.mp4 --out words.json
```

### Choosing a provider

| `CLIPFORGE_TRANSCRIBE_PROVIDER` | What it is |
|---|---|
| `local_whisper` | faster-whisper on this machine. No per-minute cost, no audio leaves the box. |
| `openai` | Any OpenAI-compatible `/audio/transcriptions` endpoint. |
| `pocketsphinx` | A bundled offline recogniser. Smoke tests only — see below. |

There is no default. An unset variable raises `ProviderUnavailable` rather
than falling back to whatever happens to be installed: a pipeline that quietly
picks a different transcriber than the operator configured is a pipeline
producing captions from a model nobody chose.

Everything else is a variable under the same prefix — model, device, compute
type, beam size, base URL, chunk length. **No key is ever read from a flag, a
config file or a constant.** The OpenAI-compatible provider is told the *name*
of the variable holding its key (`CLIPFORGE_TRANSCRIBE_API_KEY_ENV`, default
`OPENAI_API_KEY`) and reads it at request time, so the key is never stored on
an object, never written to a run record, and never appears in
`describe_environment()` — which reports the variable's name and whether it is
set, and never its value.

### Long media, bounded memory

A three-hour podcast is not loaded into memory, and neither is a thirty-second
one. ffmpeg extracts 16 kHz mono PCM to disk, anything over fifteen minutes is
cut into ten-minute chunks with three seconds of overlap, and each chunk is
deleted the moment it has been transcribed — so peak disk is one chunk, not the
whole recording. The scratch directory is removed in a `finally`, which is the
path that matters: a failure halfway through a long file has hundreds of
megabytes to answer for.

Overlapping chunks are stitched on the **midpoint of each word**, not on the
chunk boundary. A word that straddles a boundary is decoded twice, once badly
at the edge of each window, and taking whichever copy has its centre inside
the keep-range picks the one the recogniser had context for.

Upload never buffers either: the OpenAI-compatible provider streams the file
into a multipart body straight off disk, computing the content length
arithmetically rather than by building the payload.

### Job states

`queued`, `processing`, `succeeded`, `failed_retryable`, `failed_permanent`.
The split matters because the two failures want opposite handling: a 429 or a
socket timeout should be tried again, and a missing model, a rejected key or a
file with no audio track will fail identically for ever. Retryable failures
back off exponentially; the queue decides when attempts are exhausted, and the
run's state follows the job's rather than guessing — a retryable error on the
final attempt is permanently failed.

One source has one run row, not one per attempt. Re-queueing after a permanent
failure — a bad key since replaced, a provider since installed — lands on the
existing row, because `unique(tenant_id, source_id)` means a fresh id would be
a constraint error raised from inside the queue.

### Read before transcribe

`EngineTranscriber` implements the factory's `Transcriber` protocol and looks
for a stored transcript before producing one. Transcription is the most
expensive stage in the pipeline and the only one whose output never changes for
a given input; a second pass is a second invoice on a paid provider and a
second transcript that disagrees at the margins with captions somebody already
reviewed. The inline path stores its result for the same reason — a transcript
that is returned but not written is one the next cycle pays for again.

### What is verified here, and what is not

The suite runs the audio path, the chunking, the merge, the states, the
retries, the persistence and the pipeline handover against real media, with
real ffmpeg, and with a real speech recogniser producing real word timings.

**Whisper itself is unverified in this environment.** The model host is
unreachable from the machine this was built on, so `local_whisper` has never
produced a transcript here, and no request has ever reached OpenAI. What is
tested for those two is everything up to the network: the OpenAI-compatible
client against a local HTTP server implementing the documented protocol —
multipart upload, `verbose_json`, `timestamp_granularities[]`, and the status
codes that decide retryable from permanent — and the faster-whisper result
mapping against its documented result shape.

`availability()` says so rather than leaving it to this file: a provider that
is installed and configured but has never run reports `unverified=True`, and
`--check` prints it.

The recogniser the tests actually run is **pocketsphinx**, which is a real
speech recogniser with real timings and accuracy well below Whisper. So the
tests assert on plumbing — units, ordering, geometry, persistence, whether the
next stage accepted the input — and never on which words came back. An
assertion on transcript content would be an assertion about pocketsphinx, and
it would fail the day the provider is swapped for the one that belongs there.

### Testing it

```sh
CLIPFORGE_FFMPEG=$(command -v ffmpeg) PYTHONPATH=src \
  python -m unittest tests.test_transcribe tests.test_end_to_end
```

`tests/test_end_to_end.py` is the one that runs the whole chain — acquisition,
transcription, clip intelligence, rendering — on one synthesised spoken file,
and ends with a measured 1080x1920 MP4 that still has its audio.

| Module | Responsibility |
|---|---|
| `types.py` | `Transcript`, `Segment`, `Word`, states, and errors split by what a caller should do. |
| `audio.py` | ffmpeg extraction, chunk planning, cleanup. |
| `provider.py` | The provider protocol, availability, and the overlap merge. |
| `whisper_local.py` | faster-whisper, with the hallucination filter. |
| `openai_api.py` | Streaming multipart upload and `verbose_json` parsing. |
| `sphinx.py` | The offline smoke-test recogniser. Never a default. |
| `config.py` | Everything the environment decides, and what it reports. |
| `engine.py` | Queue, states, retries, persistence. |
| `pipeline.py` | The factory's `Transcriber`, backed by all of the above. |

## The upload transport

Adapters build requests; this is the layer that sends them. Until it existed,
the only `Transport` in the repository was a scripted test double and **nothing
had ever been uploaded to any platform**.

```python
from clipforge.publish import (
    AccountManager, HttpTransport, PublishingSystem, TokenRefresher,
)

transport = HttpTransport()
refresher = TokenRefresher(transport, token_store, credentials)
system = PublishingSystem(token_store=token_store, refresher=refresher)
system.tick(transport)          # one pass of the worker loop
```

```sh
python demo/run_upload_demo.py --all --verify   # a real upload over a socket
python demo/run_upload_demo.py --refresh        # the token lifecycle
python demo/run_upload_demo.py --failures       # what each failure becomes
```

### `http.client`, not `urllib`

`urllib.request` follows redirects and raises on 4xx, and both are wrong here.

**308 is not a redirect.** Google's resumable protocol uses `308 Resume
Incomplete` to mean "still going, here is how much I have" — the single most
important status in the whole upload. A client that follows it re-sends the
chunk to a URL that was never meant to receive it.

**4xx is data.** `retry.py` turns a 401 into REAUTH, a 429 into a delay taken
from `Retry-After`, and a 400 into a permanent failure — using the error code
in the body. An exception loses the body.

### Retries stop at the connection

The transport retries exactly one thing: establishing a connection, before any
byte of the request body has been sent. That is safe, because the platform has
not been told anything.

Once bytes are on the wire it gives up and reports, because it cannot know
whether the platform processed them — and `retry.py` can, via
`already_in_flight`, which is the difference between RETRY and RECONCILE. A
transport that helpfully retried a POST would silently turn the ambiguous case
into a double post, which is the failure this system is most careful to avoid.

Failures are typed by what a caller can do about them: `TimeoutError` (may have
been acted on) and `TransportError` (nothing was delivered) are different
answers, and `retry.py` treats them differently.

### Memory is flat regardless of chunk size

A chunk is streamed off disk through a bounded reader, so a 64 MB TikTok chunk
never exists in memory. Slicing it into a `bytes` would make peak memory the
platform's chunk size times the worker count, which is how a four-worker box
dies on a long podcast.

### Token refresh

Renewed *before* the upload, not at expiry. A resumable upload runs for
minutes, and a token that was valid when the session opened can expire while
the third chunk is in flight — which surfaces as a 401 halfway through a file
that is already half on the platform.

The two failures are kept apart, because they need opposite handling: a
rejected grant (`invalid_grant`, a revoked app) raises `ReauthRequired` and
asks for a human, while an undeliverable refresh (timeout, 503) raises
`RefreshFailed` and is retried. Conflating them produces either a system that
never asks you to reconnect or one that asks constantly.

Refresh is serialised per account. Several platforms retire the old refresh
token on use, so two workers refreshing at once leaves the loser holding a
credential the platform has already invalidated — an account that then fails
every post with credentials that look present and valid.

### Verification is a separate read

The state machine reports DONE when the protocol says the upload finished. That
is not the same claim as "the video is on the account":

- YouTube's `uploadStatus` can become `rejected` — duplicate, claimed audio —
  minutes after a perfectly successful upload, and nothing pushes that back.
- TikTok returns `PUBLISH_COMPLETE`, but a moderation failure arrives as
  `FAILED` and the post never appears.

`UploadVerifier` reads the post back. Every request it makes is a GET or a
status POST that creates nothing, which is what makes it safe on a timer and
safe after a timeout. `Verification.unknown` is deliberately not `rejected`:
treating an outage as a missing post would re-upload videos that are live.

### Account management

`AccountManager` owns the two halves of the OAuth flow and the gap between
them. The `state` is checked rather than trusted — an unchecked `state` is the
CSRF hole in every OAuth integration that has one, letting an attacker connect
*their* account to the victim's channel so every subsequent post goes to the
attacker's audience. Verifiers are single-use and pending connections expire.

Disconnect revokes before it forgets. Deleting a token locally leaves a live
grant on the platform. A revocation failure does not block the local delete —
the operator asked to disconnect, and refusing because the platform is down
leaves them unable to act — but it is reported.

### What is verified, and what is not

`tests/test_publish_transport.py` runs a real `ThreadingHTTPServer` per
platform and drives the production client at it over TCP: real framing, real
streamed bodies, real status codes, byte-for-byte comparison of what the
server received against what is on disk. The servers are strict, and reject
what the platforms reject — a non-contiguous chunk, a `Content-Range` that
disagrees with the body, a chunk count that differs from what was declared.

**No upload has reached a real platform.** Outbound CONNECT to
`open.tiktokapis.com` and `graph.facebook.com` is refused by policy in the
environment this was built in, and there are no credentials for any of the
three. One test does leave the machine: `LiveGoogleTokenTest` sends an invalid
refresh to Google's real token endpoint over real TLS and asserts the reply is
understood — which verifies TLS, form encoding, a genuine Google error body,
and the dead-grant path. Run it with `CLIPFORGE_LIVE_OAUTH=1`.

So: the client is correct against the documented protocols, and unproven
against the platforms themselves. The first real credential will find things.

| Module | Responsibility |
|---|---|
| `transport.py` | The HTTP client. TLS, streamed ranges, typed failures. |
| `refresh.py` | Token renewal, and the difference between the two failures. |
| `verify.py` | Reading a post back to see whether it really exists. |
| `accounts.py` | Connect, reconnect, disconnect, health. |
| `adapters.py` | The three protocols, as pure state machines. |
| `retry.py` | What a failure means and when to come back. |

## Authentication

Email and password, JWT access tokens, rotating refresh tokens, verification
and reset links, rate limiting and an audit trail, on PostgreSQL. Real bcrypt,
real PyJWT — no hand-rolled crypto anywhere in it.

```python
from clipforge.auth import AccessTokenIssuer, AuthService, config_from_env

config = config_from_env()
config.require_production_ready()        # refuses the unsafe defaults
service = AuthService(store, AccessTokenIssuer(config.keyring), config=config)

service.sign_up("dana@example.com", "marmalade tuesday bicycle")
service.verify_email(token_from_the_link)
result = service.log_in("dana@example.com", "marmalade tuesday bicycle")
principal = service.authenticate(result.tokens.access_token)
```

```sh
python demo/run_auth_demo.py --all       # every flow, and what it refuses
```

### An identity is not a user

`users` stays tenant-scoped — `unique(tenant_id, email)`, because a global
unique would leak one customer's staff list to another's signup form. But
authentication happens *before* a tenant is known, and asking for a workspace
slug before the password is a cost paid by every user on every login to solve a
problem the rare multi-workspace person has.

So an **identity** is the human — one global email, one password, one
verification state — and a **user** is a membership: that human's role inside
one tenant. One identity, many memberships, which is also what happens when an
agency operator works across four client workspaces. Access tokens are minted
per tenant, so a session is always unambiguous about which workspace it is in.

The leak the original comment worried about is handled where it actually lives:
signup, login and password reset return byte-identical answers whether or not
the address is registered, and take the same time doing it.

### The request path cannot read a password hash

The five `auth_*` tables are granted to a fourth role, `clipforge_auth`, and
`clipforge_app` — what every request runs as — is granted **nothing** on them.
Migration 002 sets `ALTER DEFAULT PRIVILEGES` so later tables are readable by
the app role, which is right for tenant data and wrong for credentials, so
migration 006 revokes it explicitly.

There are no RLS policies here, and that is not an omission: RLS scopes rows to
`app.current_tenant()`, and there is no tenant at login. The grant is the
boundary instead, and it is stronger — the app role cannot see these rows at
any tenant setting. Verified directly:

```
clipforge_app  → SELECT FROM auth_identities   ERROR: permission denied
clipforge_auth → SELECT FROM clips             ERROR: permission denied
clipforge_auth → UPDATE auth_audit_log         ERROR: permission denied
```

The audit log is append-only as a privilege, not a convention: a log the
authenticating service can rewrite says whatever an attacker who reached that
service wants it to say.

### bcrypt truncates at 72 bytes; this does not

bcrypt ignores everything past the 72nd byte. Older releases did it silently,
turning a 100-character passphrase into a 72-character one; bcrypt 5 raises,
which is better and still the wrong answer on a signup form.

So the password is SHA-256'd and base64'd before it reaches bcrypt — a fixed
44-byte input with the full entropy preserved. The base64 matters as much as
the hash: a raw digest can contain a NUL byte, and bcrypt truncates at the
first one, which would quietly weaken about one hash in 180.

Hashes are upgraded on login. The cost that is right today will be too low in
2030, and the successful-login path is the one moment the plaintext is
legitimately in memory. The fleet migrates itself; nobody is asked to reset
anything.

### Refresh token reuse is treated as theft

Access tokens are JWTs because they must be checkable without a database round
trip. That is also why they cannot be revoked, which is why they live fifteen
minutes.

Refresh tokens are 256 bits of opaque randomness, stored as SHA-256, rotated on
every use. Present a token that has already been rotated away from and exactly
one of two things happened: a client raced itself, or a copy is in circulation
and the real client has rotated past it. They are indistinguishable at the time
and the second is far worse, so **the entire session family is revoked**. The
legitimate user signs in again; the thief's copy is dead.

Sessions also carry an absolute ceiling, not just an idle timeout. Without one,
a session that keeps refreshing lives for ever — and so does a stolen one.

### Two rate limits, because they stop different attacks

Per-IP stops one host working through a list of addresses. Per-address stops a
botnet spread over thousands of hosts grinding one account. Either alone leaves
the other wide open. Both are durable in Postgres, incremented by a single
upsert — the read-then-write version loses increments under exactly the load a
limiter exists for.

The account lockout sits on top and is deliberately *not* reachable in one
burst: the rate limiter stops a burst, and the lockout catches an attacker
patient enough to spread attempts across windows.

### What a deployment must set

`AuthConfig.require_production_ready()` refuses to start on any of: a signing
key generated at startup, bcrypt cost under 10, sign-in without email
verification, rate limiting disabled, access tokens over an hour, or `http://`
links. Keys come from `CLIPFORGE_AUTH_SIGNING_KEYS` as `kid:secret,kid:secret`
— several, so a key can be rotated without ending every session.

`db/roles.sql` gained `clipforge_auth` and needs re-running before migration
006 applies; `PGAUTH_PASSWORD` is the new variable.

### What is verified, and what is not

`tests/test_auth.py` runs 156 tests — every flow twice, once in memory and once
against PostgreSQL, with real bcrypt and real PyJWT throughout. Beyond the
feature coverage it asserts the security properties directly: identical
enumeration responses, comparable timing on unknown addresses, refresh-reuse
family revocation, single-use links, session revocation on reset, `alg=none`
and cross-key and cross-audience token rejection, and that no token or password
ever reaches the audit log.

**No email has ever been sent.** `RecordingEmailSender` is the default and
records messages instead of delivering them. `SmtpEmailSender` speaks real SMTP
and no server has accepted a message from it here. Until that is wired to a
provider, a deployment registers users who never receive a verification link —
which is why the default records rather than silently swallows.

**There is still no HTTP layer.** This is a library: it verifies a token and
returns a `Principal`. Nothing in the repository yet turns an HTTP request into
a call on it, so "authentication" is complete and "authenticated API" is not.

| Module | Responsibility |
|---|---|
| `types.py` | Identity, session, event, and errors phrased for the end user. |
| `passwords.py` | bcrypt, the length-safe pre-hash, and the policy. |
| `tokens.py` | JWT issuing and verification; opaque refresh tokens. |
| `service.py` | Every flow, and the enumeration and reuse defences. |
| `store.py` | The persistence protocol, and an in-memory implementation. |
| `postgres.py` | The real store, as `clipforge_auth`. |
| `email.py` | Message templates and the senders. Nothing is delivered. |
| `config.py` | What a deployment sets, and what it is refused. |

## The API, and the dashboard on top of it

Until this existed, every layer in this repository was a library that nothing
turned an HTTP request into a call on. `authenticate()` returned a `Principal`
and no request ever produced one.

```sh
# 1. the API
CLIPFORGE_DSN=postgresql://clipforge_app:...@host/clipforge \
CLIPFORGE_AUTH_DSN=postgresql://clipforge_auth:...@host/clipforge \
CLIPFORGE_AUTH_SIGNING_KEYS="k1:$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  python -m clipforge.api.server

# 2. some real rows to look at
python demo/seed_dashboard.py --dsn ... --auth-dsn ...

# 3. the dashboard
cd web && npm install && npm run dev      # http://localhost:5173
```

`--in-memory` runs the API with nothing persisted. It must be asked for: the
alternative — falling back when a DSN is missing — is a deployment that starts
cleanly, serves requests and loses everything on restart with no error anywhere.

### The tenant comes from the token

There is no `?tenant_id=` anywhere in the API and there must never be. The
tenant is a claim inside a signed access token, so reaching another customer's
data means forging a signature rather than editing a URL. Every read then goes
through `unit_of_work(principal.tenant_id)`, which sets `app.tenant_id` for the
transaction and puts row-level security underneath the Python — two independent
checks, and the database's one does not trust the application at all.

`TenantIsolationTest` signs in as one tenant and goes looking for the other's
rows, because "there is no parameter to tamper with" is an argument and not
evidence.

### Errors have one shape

Every failure leaves as `{"error": {"code", "message"}}`, whatever it started
as. `code` is stable and for machines; `message` is for a person. An unhandled
exception becomes a generic 500 whose body says nothing about the failure — a
stack trace in a response is a map of the system for anyone who can provoke one.
The shape is declared in the OpenAPI document, so it reaches the generated
TypeScript too.

### Types cannot drift

`web/src/api/types.ts` is generated from `/api/v1/openapi.json`. Rename a field
in `schemas.py`, regenerate, and every page that used the old name fails `tsc`.
The alternative is a hand-written interface that agrees with the backend until
one day it does not, and then renders `undefined` into a cell.

Response models are deliberately *not* the store records: a record is an
internal shape that changes with the schema, and returning one directly makes a
column rename a breaking change for every client — and a column added a leak
nobody reviewed.

### Null is not zero

Most metric fields are nullable and the dashboard renders `—`. No live metric
source is wired up, so a published post usually has no measurement, and
charting that as zero would be a claim about the video rather than about the
collection. `/api/v1/analytics` carries a `note` explaining itself rather than
returning a flat line.

### What the dashboard admits it cannot do

The Settings page ends with a capability list, and most of its answers are
negative: no object storage, no live metrics, no email delivery, no acquisition
worker. Each explains a way the product will appear broken. A dashboard that
hid them would show an upload queue that never drains and give no clue why.

### Verified

`tests/test_api.py` — 39 tests over the real ASGI stack with real signed
tokens: authentication on every endpoint, cross-tenant reads and writes, role
enforcement, the error envelope, pagination bounds, and that a validation error
never echoes the password it rejected.

The dashboard was driven in a real Chromium against the running API and live
PostgreSQL: all seven pages rendered real rows, 25 API calls, no non-2xx, no
console errors. Clicking **Retry** on a failed post and **Pause** on a channel
both changed the rows in Postgres — the UI writes through the API to the
database, with nothing stubbed in between.

Writing those tests found two real bugs: `failed` uploads were excluded from
the queue, which made the Retry button unreachable from the only page that
offers it; and the overview counted acquisition `complete` when the engine
actually writes `ready`, so that stage always read zero.

| Module | Responsibility |
|---|---|
| `api/app.py` | The app factory, error handlers, CORS, health. |
| `api/deps.py` | Bearer auth to a `Principal`, tenant-scoped unit of work. |
| `api/schemas.py` | The wire contract, and the source of the TypeScript. |
| `api/routes/` | One router per page, plus auth. |
| `api/server.py` | The runnable process. |

## Media storage

Durable media lives in Cloudflare R2. Local disk is scratch.

```sh
CLIPFORGE_STORAGE_BACKEND=r2 \
CLIPFORGE_R2_BUCKET=clipforge-media \
CLIPFORGE_R2_ACCOUNT_ID=... \
CLIPFORGE_R2_ACCESS_KEY_ID=... \
CLIPFORGE_R2_SECRET_ACCESS_KEY=... \
CLIPFORGE_R2_PUBLIC_BASE_URL=https://media.example.com \
  python -m clipforge.api.server
```

### The migration is a change of system of record, not a deletion of paths

ffmpeg reads files, yt-dlp writes them, and the MP4 box reader seeks. None of
them speaks S3, and a FUSE mount would turn every seek into a range request —
a codec probes a file dozens of times before decoding a frame, so the result
works in testing and takes four minutes to start in production.

So R2 holds the durable copy, local disk holds a working copy for as long as a
job is running, and `Workspace` is the only place the two meet:

```python
with Workspace(storage, tenant_id) as work:
    media = work.fetch(source_ref)        # object → scratch file
    output = work.path("clip.mp4")
    ...ffmpeg...
    ref = work.publish(output, work.key("renders", clip_id, "clip.mp4"))
```

Anything on local disk after a job finishes is a bug. `sweep()` finds it, and
it exists because a worker killed mid-job — a reclaimed spot instance, an OOM,
a deploy — never reaches its cleanup.

### Keys start with the tenant

`ten_acme/renders/cl_123/clip.mp4`, and `key_for` is the only way to build one.
Not for tidiness: a bug that builds a wrong key lands inside the same tenant's
prefix, an IAM policy can be scoped by prefix, and deleting a customer is a
prefix delete rather than a query. `key_for` refuses traversal, because `a/../b`
is a *different object* from `b` in an object store rather than the same one —
there are no directories for the store to normalise.

### Both forms of `media_path` keep working

`StorageRef` parses `r2://bucket/key` and a bare filesystem path. Every row
written before the migration holds the latter, and a migration that required a
completed backfill before anything ran is one that gets deferred and then run
under pressure. `migrate.backfill()` moves them when convenient, is idempotent,
and leaves the local file alone unless asked twice.

If an upload fails after a successful download, the acquisition records the
local path rather than failing: the expensive half of the work succeeded, and
throwing it away to protect the cheap half is the wrong trade.

### R2 is S3-compatible, not S3

Three differences, all of which fail at the endpoint rather than at the signer:

- **`region` must be `auto`.** A real region signs fine and is then rejected.
- **No ACLs.** `ACL="public-read"` fails; public access is a bucket setting
  with a domain in front, which is why `public_url` is configured and never
  derived. A guessed URL that 403s surfaces as "Instagram could not fetch the
  media" and sends the next person to debug Meta's API instead of this setting.
- **Lifecycle is the bucket's job.** `lifecycle.RULES` describes what this
  system wants and `apply()` sets it; expiry then runs whether or not a worker
  is alive. Renders and transcripts never expire — a source is gigabytes and
  reproducible, a transcript is kilobytes and cost real inference time.

Every rule aborts incomplete multipart uploads, which are invisible in a
listing and still billed. That is the classic S3 cost leak.

### Retries, and what is not retried

boto3's adaptive retry is left on and the loop here sits above it for what
botocore does not treat as retryable but which is worth one more attempt — a
connection reset mid-upload most of all. Writes are retried too, safely: every
write is an idempotent PUT to a key the caller chose, so repeating one
overwrites the same object with the same bytes. A 403 is not retried, because
four attempts at a bad credential only delay the useful error.

Metrics count retries separately from failures. Failures rising means something
is broken; retries rising while failures stay flat means something is degraded
and the retry budget is absorbing it — the state worth catching before it
becomes the first.

### What is verified, and what is not

`tests/test_storage.py` — 60 tests. One contract runs over both backends, and
the R2 side runs against a real S3 server (moto) over HTTP with real boto3:
round trips, multipart above the threshold, presigned GET and PUT, prefix
listing and deletion, usage, lifecycle configuration, the retry loop and the
permanent/transient split.

**No byte has reached Cloudflare.** `*.r2.cloudflarestorage.com` is refused by
this environment's egress policy — a 403 to CONNECT — and there are no R2
credentials. What is proven is that this client is correct against the S3 API,
not that R2 behaves as documented.

**moto does not validate signatures.** A presigned URL with a tampered key
returns 404 rather than 403, so these tests exercise presigning as URL
construction and not as authentication. The reason to trust the signing is that
boto3 does it.

| Module | Responsibility |
|---|---|
| `types.py` | Keys, refs, errors split by what a caller can do. |
| `protocol.py` | The interface, and the metrics every backend reports. |
| `local.py` | A directory. Still the right choice for one machine. |
| `r2.py` | boto3 against R2, with retries and multipart. |
| `workspace.py` | The seam between objects and the tools that need files. |
| `lifecycle.py` | What is kept, for how long, and who deletes it. |
| `migrate.py` | Wiring into the engines, and the backfill. |
