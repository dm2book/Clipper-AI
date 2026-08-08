"""Caption engine demo — all five languages, all five styles.

    python demo/run_caption_demo.py               # all languages, punch style
    python demo/run_caption_demo.py --styles      # one language, every style
    python demo/run_caption_demo.py --lang de --export ass
    python demo/run_caption_demo.py --json

Word timings are synthesised from word length, which is what a real ASR
produces to within a few tens of milliseconds and is enough to exercise every
code path in the engine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.captions import (  # noqa: E402
    CaptionConfig,
    CaptionEngine,
    Language,
    PRESETS,
    TimedWord,
    to_ass,
    to_srt,
    to_vtt,
)

# Each sample is written to exercise that language's specific rules, not just
# to be a translation of the English one.
SAMPLES: dict[Language, list[tuple[str, str]]] = {
    Language.ENGLISH: [
        ("HOST", "So how much money did you actually lose in the end?"),
        ("GUEST", "Eighteen million. That was the biggest mistake I ever made."),
        ("GUEST", "Nobody tells you the secret. The money is just a deadline."),
    ],
    Language.DUTCH: [
        ("HOST", "Dus hoeveel geld heb je uiteindelijk verloren?"),
        ("GUEST", "Achttien miljoen. Dat was de grootste fout die ik ooit maakte."),
        ("GUEST", "'s Ochtends besefte ik het pas. De arbeidsongeschiktheidsverzekering was waardeloos."),
    ],
    Language.GERMAN: [
        ("HOST", "Wie viel Geld hast du am Ende tatsächlich verloren?"),
        ("GUEST", "Achtzehn Millionen. Das war der größte Fehler meines Lebens."),
        ("GUEST", "Die Rechtsschutzversicherung hat überhaupt nichts gebracht."),
    ],
    Language.FRENCH: [
        ("HOST", "Alors combien d'argent as-tu vraiment perdu ?"),
        ("GUEST", "Dix-huit millions. C'était la plus grosse erreur de ma vie."),
        ("GUEST", "Personne ne te dit le secret : l'argent, c'est juste une échéance."),
    ],
    Language.SPANISH: [
        ("HOST", "¿Cuánto dinero perdiste realmente al final?"),
        ("GUEST", "Dieciocho millones. Fue el mayor error de mi vida."),
        ("GUEST", "Nadie te cuenta el secreto: el dinero es solo un plazo."),
    ],
}


def synthesise(lines: list[tuple[str, str]], wps: float = 2.9) -> list[TimedWord]:
    """Turn (speaker, sentence) pairs into word-level timings."""
    words: list[TimedWord] = []
    cursor = 400
    for speaker, sentence in lines:
        for token in sentence.split():
            # Longer words take longer to say; punctuation adds a beat.
            duration = int(max(180, len(token) / wps * 1000 * 0.55))
            words.append(
                TimedWord(text=token, start_ms=cursor, end_ms=cursor + duration,
                          speaker=speaker)
            )
            cursor += duration + (55 if not token[-1] in ".!?:" else 320)
        cursor += 260  # pause between sentences
    return words


def timeline(word, cue_start: int, cue_end: int, width: int = 26) -> str:
    span = max(1, cue_end - cue_start)
    lo = int((word.start_ms - cue_start) / span * width)
    hi = max(lo + 1, int((word.end_ms - cue_start) / span * width))
    return "·" * lo + "█" * (hi - lo) + "·" * max(0, width - hi)


def show(track, label: str) -> None:
    stats = track.stats
    print(f"\n  {label}")
    print(f"  {'─' * 68}")
    print(f"  {stats['cues']} cues · {stats['words']} words · "
          f"{stats['speakers']} speakers · {stats['emoji_added']} emoji · "
          f"{stats['cues_shrunk']} shrunk · {stats['elapsed_ms']}ms")
    print()
    for cue in track.cues:
        color = track.speaker_colors.get(cue.speaker, "")
        tag = f"{cue.speaker}{' ' + color if color else ''}"
        scale = f"  [scaled {cue.font_scale:.2f}]" if cue.shrunk else ""
        print(f"   {cue.index:>2}. {cue.start_ms / 1000:6.2f}s "
              f"→{cue.end_ms / 1000:6.2f}s   {tag}{scale}")
        for line in cue.lines:
            print(f"       “{line.text}”   ({line.width_em:.1f} em)")
        for word in cue.words:
            mark = "◆" if word.is_emoji else " "
            print(f"        {mark} {timeline(word, cue.start_ms, cue.end_ms)} "
                  f"{word.text}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=[l.value for l in Language])
    parser.add_argument("--style", default="punch", choices=sorted(PRESETS))
    parser.add_argument("--styles", action="store_true", help="compare every style")
    parser.add_argument("--export", choices=["ass", "vtt", "srt"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.styles:
        language = Language(args.lang) if args.lang else Language.ENGLISH
        words = synthesise(SAMPLES[language])
        print(f"\n  STYLE COMPARISON — {language.value}\n")
        for name in sorted(PRESETS):
            style = PRESETS[name]
            track = CaptionEngine(
                CaptionConfig(style=style, language=language)
            ).generate(words)
            grouping = f"{style.max_words}w × {style.max_lines}L"
            print(f"  {name:<11} {style.font_size_px:>3}px  {grouping:<8} "
                  f"{style.animation.value:<11} {len(track.cues):>2} cues   "
                  f"e.g. “{track.cues[0].text}”")
        print()
        return 0

    languages = [Language(args.lang)] if args.lang else list(Language)
    style = PRESETS[args.style]

    for language in languages:
        words = synthesise(SAMPLES[language])
        track = CaptionEngine(
            CaptionConfig(style=style, language=language)
        ).generate(words)

        if args.json:
            print(json.dumps(track.to_dict(), ensure_ascii=False, indent=2))
            continue

        if args.export:
            exporter = {"ass": lambda: to_ass(track, style),
                        "vtt": lambda: to_vtt(track),
                        "srt": lambda: to_srt(track)}[args.export]
            print(exporter())
            continue

        from clipforge.captions import rules_for
        rules = rules_for(language)
        show(track, f"{rules.name}  ({language.value})  ·  style: {style.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
