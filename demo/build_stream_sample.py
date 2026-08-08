"""Generate a synthetic Twitch VOD chat log for the stream clipper demo.

Produces `demo/sample_stream.json` in raw Twitch export shape, so the demo
exercises the real adapter rather than hand-built `ChatMessage` objects.

Chat is modelled as a Poisson-ish baseline of ordinary chatter with reaction
bursts layered on top at scripted moments. Each burst carries the emote profile
that moment would actually produce — a rage moment is MALDING and angry emoji,
not generic excitement — and starts a few seconds *after* the scripted moment,
because that is what real chat does. Recovering that offset is exactly what the
clipper has to get right.

Run: python demo/build_stream_sample.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20260808
STREAM_SECONDS = 2_400  # 40 minutes
BASELINE_PER_SECOND = 1.8

# Lag between the on-screen moment and chat reacting to it. The clipper does
# not get told this — it has to infer it from the platform default.
CHAT_LAG_S = 4.5

FILLER = [
    "yeah", "true", "hi chat", "what game is this", "first time here",
    "lets go boys", "how long has he been live", "gm", "o7", "hi",
    "whats the setup", "any tips", "im new here", "sub hype", "nice",
    "can you play the other map", "hello from brazil", "what rank is he",
    "lurking", "back", "brb food", "who else watching at work",
]

# (offset_s, label, intensity, [(message, weight), ...])
MOMENTS: list[tuple[int, str, float, list[tuple[str, int]]]] = [
    (180, "funny", 1.0, [
        ("KEKW", 30), ("KEKW KEKW KEKW", 12), ("OMEGALUL", 18), ("LULW", 10),
        ("ICANT", 8), ("\U0001F602\U0001F602", 10), ("im crying", 4),
        ("KEKWWWWW", 6), ("\U0001F480", 6), ("chat he did not just say that", 3),
    ]),
    (420, "rage", 0.85, [
        ("MALDING", 26), ("Madge", 14), ("he is TILTED", 8), ("mald", 10),
        ("\U0001F92C", 8), ("SEETHING", 6), ("calm down", 5), ("RAGEY", 6),
        ("OMEGALUL he's malding", 6), ("chill bro", 4),
    ]),
    (780, "win", 1.0, [
        ("POGGERS", 28), ("PogChamp", 16), ("EZ Clap", 8), ("LETSGOOO", 14),
        ("W", 12), ("\U0001F525\U0001F525", 10), ("CLUTCH", 10), ("insane", 8),
        ("no way he hit that", 5), ("POGGERSSSS", 7),
    ]),
    (1150, "fail", 0.9, [
        ("OMEGALUL", 26), ("L", 16), ("he threw", 10), ("choked", 9),
        ("Sadge", 12), ("F", 10), ("KEKW he threw it", 8), ("oof", 7),
        ("how do you miss that", 4), ("\U0001F480", 8),
    ]),
    (1500, "argument", 0.8, [
        ("COPE", 20), ("ratio", 12), ("he's actually wrong here", 6),
        ("Clueless", 10), ("copium", 12), ("nah he has a point", 6),
        ("chat is split", 3), ("cringe", 7), ("based", 8), ("touch grass", 4),
    ]),
    (1900, "emotional", 0.75, [
        ("PepeHands", 22), ("widepeepoSad", 12), ("FeelsBadMan", 10),
        ("\U0001F97A", 9), ("respect", 8), ("this got real", 4),
        ("Sadge", 10), ("\U0001F62D", 8), ("wholesome", 5), ("❤", 6),
    ]),
    (2150, "donation", 0.7, [
        ("POGGERS", 12), ("what a legend", 8), ("thats a lot of bits", 6),
        ("\U0001F525", 8), ("actual W", 6), ("respect", 6), ("PogU", 8),
    ]),
]

EVENTS = [
    {"type": "cheer", "offset_seconds": 778, "user_name": "kx_flick", "bits": 1000,
     "message": "that was disgusting, take my bits"},
    {"type": "subscription", "offset_seconds": 905, "user_name": "mira_ttv", "tier": "1000"},
    {"type": "cheer", "offset_seconds": 2148, "user_name": "oldheadgg", "bits": 25000,
     "message": "been watching since 2019, this stream got me through a rough year"},
    {"type": "subgift", "offset_seconds": 2160, "user_name": "oldheadgg", "amount": 25.0},
    {"type": "raid", "offset_seconds": 2380, "user_name": "smallstreamer42"},
]


def message(offset: float, author: str, body: str) -> dict:
    """One Twitch VOD comment.

    Emote fragments are emitted for known emote-shaped tokens so the adapter's
    structured-emote path is exercised, not just the text fallback.
    """
    fragments = []
    for token in body.split():
        looks_like_emote = token[:1].isupper() and token.isalnum() and len(token) > 1
        if looks_like_emote:
            fragments.append({"text": token, "emoticon": {"emoticon_id": "1"}})
        else:
            fragments.append({"text": token})
    return {
        "content_offset_seconds": round(offset, 2),
        "commenter": {"display_name": author},
        "message": {"body": body, "fragments": fragments, "user_badges": []},
    }


def build() -> dict:
    rng = random.Random(SEED)
    comments: list[dict] = []
    users = [f"user_{i:03d}" for i in range(400)]

    # Baseline chatter across the whole stream.
    for second in range(STREAM_SECONDS):
        for _ in range(rng.poisson(BASELINE_PER_SECOND) if hasattr(rng, "poisson")
                       else _poisson(rng, BASELINE_PER_SECOND)):
            comments.append(
                message(second + rng.random(), rng.choice(users), rng.choice(FILLER))
            )

    # Reaction bursts, offset by the chat lag.
    for offset_s, label, intensity, pool in MOMENTS:
        weighted: list[str] = []
        for text, weight in pool:
            weighted.extend([text] * weight)

        burst_start = offset_s + CHAT_LAG_S
        # Bursts decay: loudest in the first couple of seconds, tailing off.
        for tick in range(14):
            decay = max(0.08, 1.0 - tick / 12.0)
            count = int(38 * intensity * decay)
            for _ in range(count):
                comments.append(
                    message(
                        burst_start + tick + rng.random(),
                        rng.choice(users),
                        rng.choice(weighted),
                    )
                )

    comments.sort(key=lambda c: c["content_offset_seconds"])
    return {
        "session_id": "demo-twitch-vod",
        "platform": "twitch",
        "duration_ms": STREAM_SECONDS * 1000,
        "source_width": 1920,
        "source_height": 1080,
        "regions": [
            {"name": "gameplay", "x": 0, "y": 0, "width": 1920, "height": 1080},
            {"name": "facecam", "x": 1420, "y": 760, "width": 480, "height": 270},
        ],
        "ground_truth_moments": [
            {"offset_s": o, "label": label} for o, label, _, _ in MOMENTS
        ],
        "chat": comments,
        "events": EVENTS,
    }


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's Poisson sampler — stdlib `random` has no Poisson."""
    import math

    target = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= target:
            return k
        k += 1


if __name__ == "__main__":
    out = Path(__file__).parent / "sample_stream.json"
    payload = build()
    out.write_text(json.dumps(payload) + "\n")
    print(
        f"wrote {out} — {len(payload['chat'])} chat messages, "
        f"{len(payload['events'])} events, {STREAM_SECONDS}s stream"
    )
