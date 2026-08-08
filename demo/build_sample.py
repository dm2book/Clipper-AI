"""Generate the sample transcript fixture.

Writes `demo/sample_transcript.json`. Timings are derived from a words-per-
second estimate so the fixture stays internally consistent when lines change —
hand-maintaining timestamps for 60 utterances is a reliable source of bugs.

Run: python demo/build_sample.py
"""

from __future__ import annotations

import json
from pathlib import Path

WORDS_PER_SECOND = 2.6
GAP_MS = 220

HOST = "HOST"
GUEST = "GUEST"

# (speaker, text) — a founder interview containing all ten signal categories
# plus a good amount of filler, because a detector that cannot ignore filler
# is not worth much.
LINES: list[tuple[str, str]] = [
    (HOST, "Welcome back to the show. I'm here with Dana, who I've been trying to get on for about a year now."),
    (GUEST, "Thanks for having me. Long overdue."),
    (HOST, "How was the flight in?"),
    (GUEST, "Fine, delayed a bit, but fine. Slept most of it."),
    (HOST, "Good. So let's start at the beginning. Walk me through the early days."),
    (GUEST, "So we started in 2019, two of us, working out of my apartment in Oakland."),
    (HOST, "And what was the product at that point?"),
    (GUEST, "Honestly it was a spreadsheet with a login screen bolted on top. That was it."),

    (GUEST, "We hit a hundred thousand dollars in revenue in the first eleven months, which nobody believed."),
    (HOST, "From a spreadsheet."),
    (GUEST, "From a spreadsheet. We scaled it to 2.4 million ARR before we wrote a single line of real backend code."),
    (HOST, "That's genuinely absurd."),
    (GUEST, "It is absurd. And it worked because we were solving something people were already doing manually."),

    (GUEST, "Then in 2021 we raised a Series A. Eighteen million dollars. That was the worst decision I have ever made."),
    (HOST, "Wait, the raise was the mistake?"),
    (GUEST, "The raise was the mistake. We went from twelve people to ninety in seven months and we almost went bankrupt doing it."),
    (GUEST, "I lost everything I'd built. The culture, the speed, all of it. We burned fourteen million dollars in nineteen months and had almost nothing to show for it."),
    (HOST, "How close did you actually get?"),
    (GUEST, "Eleven days of runway. I was terrified. I genuinely thought I was going to have to tell ninety people they were out of a job before Christmas."),

    (GUEST, "Here's what nobody tells you about venture funding. The money isn't the product. The money is a deadline."),
    (GUEST, "I've never said this publicly, but our board pushed us to hire a VP of Sales who cost us about eight months and two million dollars, and I knew it was wrong when I signed the offer."),
    (HOST, "Why did you sign it then?"),
    (GUEST, "Because I was twenty-nine and I thought the adults in the room knew something I didn't. They did not."),

    (GUEST, "Unpopular opinion: most seed-stage startups should never raise institutional money at all."),
    (HOST, "That's going to annoy a lot of your investors."),
    (GUEST, "Probably. But it's true. Venture capital is a specific instrument for a specific shape of business and everyone treats it like it's oxygen."),
    (HOST, "I think that's completely wrong, actually."),
    (GUEST, "Go on."),

    (HOST, "No, that's not what the data says. The companies that get to real scale are overwhelmingly venture backed."),
    (GUEST, "You're missing the point. I'm not saying nobody should raise."),
    (HOST, "That is what you said. You said most startups shouldn't raise."),
    (GUEST, "Let me finish. I said most seed-stage startups. There's a difference and you're flattening it."),
    (HOST, "With all due respect, that's a distinction you invented thirty seconds ago."),
    (GUEST, "It's a distinction that cost me eighteen million dollars to learn, so I'd argue I've earned it."),

    (HOST, "Okay, fair enough. Let me steelman your position for a second."),
    (GUEST, "Please."),
    (HOST, "The counterargument to my point is that survivorship bias makes venture look better than it is. We only ever see the ones that worked."),
    (GUEST, "That's exactly it. On the other hand, I take your point that at a certain scale you genuinely cannot self-fund."),
    (HOST, "Right, so where I'd push back is on the timing, not the principle."),
    (GUEST, "Both things can be true. Raise late, raise less, raise from people who've operated."),

    (GUEST, "You know what the funniest part of the near-death experience was?"),
    (HOST, "I can't imagine anything about that being funny."),
    (GUEST, "We had this all-hands where I was going to tell everyone how bad it was, and the projector broke."),
    (GUEST, "So I'm standing there with a slide deck about our imminent collapse and the IT guy is crawling under the table going, is it plugged in? [laughs]"),
    (HOST, "[laughs] Oh, that's terrible."),
    (GUEST, "It was terrible. It was also the moment I stopped panicking, weirdly."),

    (HOST, "So what actually saved you?"),
    (GUEST, "We cut sixty percent of the team in one day and went back to the spreadsheet."),
    (GUEST, "Not literally, but philosophically. We asked what the twelve of us would build, and we built that."),
    (HOST, "And that worked."),
    (GUEST, "We were profitable fourteen months later and we've never raised again."),

    (GUEST, "The lesson here is that headcount is not progress. I confused the two for about two years and it nearly killed the company."),
    (GUEST, "If I could go back, I'd tell myself to stay small for twice as long as feels comfortable."),
    (HOST, "That's good advice."),
    (GUEST, "My advice would be simpler than that, actually. Before you hire anyone, write down what specifically gets worse if you don't."),
    (GUEST, "If you can't answer that in one sentence, you don't need the hire. You need a decision you're avoiding."),

    (HOST, "Last question. Was it worth it?"),
    (GUEST, "I'll never forget standing in the parking lot after the layoffs. That was the hardest day of my life, and I did it to myself."),
    (GUEST, "So, worth it? The company survived. But I don't think I get to call it worth it. I think I just get to not do it again."),
    (HOST, "Dana, thank you. This was great."),
    (GUEST, "Thanks for having me."),
]


def build() -> dict:
    utterances = []
    cursor = 1_000
    for speaker, text in LINES:
        duration = max(1_400, int(len(text.split()) / WORDS_PER_SECOND * 1000))
        utterances.append(
            {
                "start_ms": cursor,
                "end_ms": cursor + duration,
                "speaker": speaker,
                "text": text,
            }
        )
        cursor += duration + GAP_MS
    return {
        "source_id": "demo-founder-interview",
        "language": "en",
        "utterances": utterances,
    }


if __name__ == "__main__":
    out = Path(__file__).parent / "sample_transcript.json"
    payload = build()
    out.write_text(json.dumps(payload, indent=2) + "\n")
    total_s = payload["utterances"][-1]["end_ms"] / 1000
    print(f"wrote {out} — {len(payload['utterances'])} utterances, {total_s:.0f}s")
