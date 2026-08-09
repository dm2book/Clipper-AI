"""Turning a plan into an ffmpeg invocation.

The plan is the product; this module is the adapter that executes it. It emits
a filtergraph, a `sendcmd` script driving the speaker crop, and the argv to run
them.

**The camera path is executed by `sendcmd`, not by expressions.** ffmpeg's
`crop` accepts runtime commands for `x` and `y`, so a piecewise path becomes a
timestamped command script. It does *not* accept a runtime change of `w`/`h` —
those would resize the filter's output mid-stream — which is the second,
independent reason the camera in `camera.py` pans but never zooms. The two
constraints agree, which is usually a sign the design is right.

The path is compact enough for this to be practical only because of the
deadband: a camera that holds still through most of a clip emits tens of
keyframes, not thousands.

**These graphs are emitted, not executed, by the test suite** — there is no
ffmpeg in the test environment, and a test that shells out to one would be an
integration test wearing a unit test's clothes. What *is* checked, by
`link_check`, is the property that actually breaks graphs in practice: every
pad label produced is consumed exactly once, and every label consumed is
produced. Dangling and doubly-consumed pads are the failure mode, and they are
checkable without decoding a frame.
"""

from __future__ import annotations

import re
from typing import Sequence

from .types import GameplayPlan, LayoutStyle, Motion

#: Encoder settings. CRF 18 at this resolution is visually transparent for
#: short-form; the platforms re-encode anyway, so the job here is to hand them
#: something clean rather than something small.
VIDEO_ARGS: tuple[str, ...] = (
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "18",
    "-pix_fmt", "yuv420p",
    "-profile:v", "high",
    "-level", "4.2",
    "-movflags", "+faststart",
)
AUDIO_ARGS: tuple[str, ...] = ("-c:a", "aac", "-b:a", "192k", "-ar", "48000")

#: Blur applied to the fill behind footage that is fitted rather than cropped.
FIT_BLUR_SIGMA = 32


def _escape(value: str) -> str:
    """Escape a value for use inside a filter argument."""
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def sendcmd_script(plan: GameplayPlan) -> str:
    """The timestamped crop commands driving the speaker camera.

    One line per keyframe. The initial position is baked into the filter's own
    arguments, so the script carries only the changes.
    """
    lines: list[str] = []
    for keyframe in plan.camera.keyframes:
        comment = keyframe.motion.value
        lines.append(
            f"{keyframe.t:.6f} crop@spk x {keyframe.x}, "
            f"crop@spk y {keyframe.y};   # {comment}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def _speaker_chain(plan: GameplayPlan, sendcmd_path: str) -> str:
    panel = plan.panel("speaker")
    assert panel is not None
    camera = plan.camera
    first = camera.keyframes[0]

    steps = [f"fps={plan.fps}"]
    if sendcmd_path and len(camera.keyframes) > 1:
        steps.append(f"sendcmd=f='{_escape(sendcmd_path)}'")
    steps.append(
        f"crop@spk=w={camera.width}:h={camera.height}:x={first.x}:y={first.y}"
    )
    steps.append(f"scale={panel.width}:{panel.height}:flags=lanczos")
    steps.append("setsar=1")
    return "[0:v]" + ",".join(steps) + "[spk]"


def _gameplay_source(plan: GameplayPlan) -> list[str]:
    """Trim and concatenate the gameplay bed into a single continuous pad."""
    timing = plan.timing
    if timing is None or not timing.segments:
        return []

    segments = timing.segments

    if len(segments) == 1:
        return [
            f"[1:v]trim=start={segments[0].in_start:.4f}:"
            f"duration={segments[0].duration:.4f},"
            f"setpts=PTS-STARTPTS[gpsrc]"
        ]

    # A filtergraph input pad may be consumed exactly once, so reading several
    # ranges out of one file needs an explicit `split` first. Writing `[1:v]`
    # on each trim looks reasonable and is rejected outright by ffmpeg.
    chains: list[str] = []
    taps = [f"s{index}" for index in range(len(segments))]
    chains.append(
        f"[1:v]split={len(taps)}" + "".join(f"[{tap}]" for tap in taps)
    )

    labels: list[str] = []
    for index, (tap, segment) in enumerate(zip(taps, segments)):
        label = f"g{index}"
        labels.append(label)
        chains.append(
            f"[{tap}]trim=start={segment.in_start:.4f}:"
            f"duration={segment.duration:.4f},"
            f"setpts=PTS-STARTPTS[{label}]"
        )

    joined = "".join(f"[{label}]" for label in labels)
    chains.append(f"{joined}concat=n={len(labels)}:v=1:a=0[gpsrc]")
    return chains


def _gameplay_chain(plan: GameplayPlan) -> list[str]:
    panel = plan.panel("gameplay")
    if panel is None:
        return []

    chains = _gameplay_source(plan)
    if not chains:
        return []

    if panel.scale_mode == "fit":
        # Footage that cannot survive a crop is scaled whole into the band and
        # the remainder is filled with a blurred, cover-cropped copy of itself.
        # Flat bars read as a mistake; a blurred fill reads as a choice.
        chains.append(f"[gpsrc]fps={plan.fps},split=2[gpbg_in][gpfg_in]")
        chains.append(
            f"[gpbg_in]scale={panel.width}:{panel.height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={panel.width}:{panel.height},"
            f"gblur=sigma={FIT_BLUR_SIGMA}[gpbg]"
        )
        chains.append(
            f"[gpfg_in]scale={panel.width}:{panel.height}:"
            f"force_original_aspect_ratio=decrease[gpfg]"
        )
        chains.append("[gpbg][gpfg]overlay=(W-w)/2:(H-h)/2,setsar=1[gp]")
    else:
        chains.append(
            f"[gpsrc]fps={plan.fps},"
            f"crop=w={panel.source_width}:h={panel.source_height}:"
            f"x={panel.source_x}:y={panel.source_y},"
            f"scale={panel.width}:{panel.height}:flags=lanczos,setsar=1[gp]"
        )
    return chains


def filtergraph(plan: GameplayPlan, sendcmd_path: str = "camera.cmd") -> str:
    """The complete `-filter_complex` string for this plan."""
    chains: list[str] = [_speaker_chain(plan, sendcmd_path)]
    chains.extend(_gameplay_chain(plan))

    if plan.style is LayoutStyle.SPEAKER_ONLY or plan.panel("gameplay") is None:
        chains.append("[spk]copy[v]")
    elif plan.style is LayoutStyle.INSET:
        speaker = plan.panel("speaker")
        assert speaker is not None
        chains.append(
            f"[gp][spk]overlay=x={speaker.x}:y={speaker.y}:"
            f"format=auto,setsar=1[v]"
        )
    else:
        chains.append("[spk][gp]vstack=inputs=2[v]")

    return ";\n".join(chains)


def command(
    plan: GameplayPlan,
    speaker_path: str,
    gameplay_path: str = "",
    output_path: str = "out.mp4",
    sendcmd_path: str = "camera.cmd",
) -> list[str]:
    """The full ffmpeg argv.

    Gameplay audio is never mapped. It competes with the speech it is supposed
    to support, and the music on most gameplay beds carries a claim of its own,
    separate from the game's.
    """
    argv = ["ffmpeg", "-y", "-i", speaker_path]
    if gameplay_path and plan.panel("gameplay") is not None:
        # No `-stream_loop`: repetition is expressed explicitly as a
        # split/trim/concat chain, so every frame used is one the plan
        # accounted for. An input-level loop would also have to be attached to
        # the right input, and attaching it to the speaker — easy to do, since
        # it precedes its own `-i` — silently repeats the person talking.
        argv.extend(["-i", gameplay_path])

    argv.extend([
        "-filter_complex", filtergraph(plan, sendcmd_path),
        "-map", "[v]",
        "-map", "0:a?",
        "-r", str(plan.fps),
        "-t", f"{plan.duration_s:.3f}",
    ])
    argv.extend(VIDEO_ARGS)
    argv.extend(AUDIO_ARGS)
    argv.append(output_path)
    return argv


_LEADING = re.compile(r"^\s*((?:\[[^\]]+\]\s*)+)")
_TRAILING = re.compile(r"((?:\s*\[[^\]]+\])+)\s*$")
_LABEL = re.compile(r"\[([^\]]+)\]")


def link_check(graph: str) -> list[str]:
    """Structural problems in a filtergraph, as human-readable strings.

    Checks the two things that actually make ffmpeg refuse a graph: a pad that
    nothing consumes, and a pad consumed more than once. Neither needs a
    decoder to detect, and both are easy to introduce when a graph is built by
    string concatenation.
    """
    produced: dict[str, int] = {}
    consumed: dict[str, int] = {}

    for chain in graph.split(";"):
        chain = chain.strip()
        if not chain:
            continue

        head = _LEADING.match(chain)
        tail = _TRAILING.search(chain)

        for label in (_LABEL.findall(head.group(1)) if head else []):
            consumed[label] = consumed.get(label, 0) + 1
        for label in (_LABEL.findall(tail.group(1)) if tail else []):
            produced[label] = produced.get(label, 0) + 1

    problems: list[str] = []

    for label, count in sorted(produced.items()):
        if count > 1:
            problems.append(f"pad [{label}] is produced {count} times")
        if label != "v" and label not in consumed:
            problems.append(f"pad [{label}] is produced but never consumed")

    for label, count in sorted(consumed.items()):
        is_input = ":" in label          # "0:v", "1:a" — graph inputs
        if not is_input and label not in produced:
            problems.append(f"pad [{label}] is consumed but never produced")
        if count > 1:
            # Input pads are not exempt. ffmpeg rejects a filtergraph that
            # names the same input twice; reading two ranges out of one file
            # requires an explicit `split`.
            problems.append(f"pad [{label}] is consumed {count} times")

    if "v" not in produced:
        problems.append("graph produces no [v] output pad")

    return problems


def summary(plan: GameplayPlan) -> dict[str, object]:
    """Render-relevant counts, for logging and for the demo."""
    camera = plan.camera
    return {
        "keyframes": len(camera.keyframes),
        "cuts": len(camera.cuts),
        "pans": sum(1 for k in camera.keyframes if k.motion is Motion.PAN),
        "hold_ratio": round(camera.hold_ratio, 3),
        "gameplay_segments": len(plan.timing.segments) if plan.timing else 0,
        "filtergraph_chains": filtergraph(plan).count(";") + 1,
    }
