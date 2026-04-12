"""Lab 3 test segment definitions and stimulus runner.

A *segment* is one reflash-plus-stimulus block in the Lab 3 grading
capture. During a single continuous recording we execute multiple
segments, each with its own reset-to-known-state (via reflash) followed
by a scripted button-press sequence and an observation window. The
video analyzer later slices the recording at the debug-LED transitions
and scores each segment independently against ``graded_items``.

The stimulus for a segment is a tiny list-of-strings DSL:

    "G"     glitch press   (2 ms  pin-low;  helper rejects as bounce)
    "S"     short press    (250 ms pin-low)
    "L"     long press     (1500 ms pin-low)
    "D<n>"  delay <n> ms   (host-side gap between presses)

Example: ``["L", "D500", "S", "D500", "S"]`` = long press, 500 ms gap,
short press, 500 ms gap, short press. Deliberately no loops, no
conditionals, no shell-out. If a segment needs 13 short presses in a
row we write them out -- it makes the segment list readable and keeps
the runner trivial.
"""

from __future__ import annotations

import dataclasses
import time
from typing import List, Optional, Sequence

from .helper_client import HelperClient


# ---------------------------------------------------------------------------
# Stimulus token rough time budgets (seconds) -- used for duration
# estimation only. Actual execution is driven by the helper blocking on
# ACKs; these numbers just let us tell ffmpeg a sane upper-bound record
# duration before we SIGINT it.
# ---------------------------------------------------------------------------

# Nominal press durations, padded with a little USB+ACK slack.
_TOKEN_TIME_S = {
    "G": 0.30,   # 2 ms pin-low + LED_MIN_MS hold (~250 ms) + ACK
    "S": 0.30,   # 250 ms press + ACK
    "L": 1.60,   # 1500 ms press + ACK
}


# ---------------------------------------------------------------------------
# Segment dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Segment:
    """One reflash + stimulus + observe block in the Lab 3 capture.

    Attributes:
        name:         Short, file-safe identifier used in logs / CSV.
        description:  Human-readable sentence for reports.
        stimulus:     Sequence of tokens in the mini-DSL above.
        warmup_ms:    Quiet time after boot and before first stimulus
                      token. Gives the student firmware a chance to
                      finish any init and land in Normal Clock Mode.
        observe_ms:   Quiet time after the last stimulus token. This is
                      the window during which the analyzer measures
                      "did the clock end up in the expected state".
        graded_items: Rubric item keys this segment is meant to validate.
                      Scoring wires up a segment's observe window to
                      these keys later. Purely informational for capture.
    """

    name: str
    description: str
    stimulus: List[str]
    graded_items: List[str]
    warmup_ms: int = 1500
    observe_ms: int = 3000


# ---------------------------------------------------------------------------
# Stimulus execution and time budgeting
# ---------------------------------------------------------------------------


def estimate_stimulus_s(tokens: Sequence[str]) -> float:
    """Return a rough upper bound for how long ``tokens`` will take (s).

    Used for ffmpeg duration budgeting; real execution blocks on helper
    ACKs. Accuracy is ±a few hundred ms per token -- good enough to keep
    us from SIGINT-ing ffmpeg too early.
    """
    total = 0.0
    for tok in tokens:
        if tok in _TOKEN_TIME_S:
            total += _TOKEN_TIME_S[tok]
        elif tok.startswith("D"):
            try:
                total += int(tok[1:]) / 1000.0
            except ValueError as e:
                raise ValueError(f"bad delay token {tok!r}") from e
        else:
            raise ValueError(f"unknown stimulus token {tok!r}")
    return total


def run_stimulus(
    helper: HelperClient,
    tokens: Sequence[str],
) -> None:
    """Execute the stimulus DSL against ``helper``.

    Blocks until every token completes. Raises whatever the helper
    raises on protocol errors -- the capture orchestrator catches it
    and logs the segment as failed.
    """
    for tok in tokens:
        if tok == "G":
            helper.glitch()
        elif tok == "S":
            helper.short_press()
        elif tok == "L":
            helper.long_press()
        elif tok.startswith("D"):
            time.sleep(int(tok[1:]) / 1000.0)
        else:
            raise ValueError(f"unknown stimulus token {tok!r}")


# ---------------------------------------------------------------------------
# Helpers for writing readable segment lists
# ---------------------------------------------------------------------------


def _shorts(n: int, gap_ms: int = 500) -> List[str]:
    """N short presses, each preceded by a delay."""
    out: List[str] = []
    for _ in range(n):
        out += [f"D{gap_ms}", "S"]
    return out


def _longs(n: int, gap_ms: int = 500) -> List[str]:
    """N long presses, each preceded by a delay.

    Used to chain "two longs to enter minute-set", "three longs to
    cycle back to normal", etc. Long presses still have their own
    1.5 s press duration -- ``gap_ms`` is only the quiet time between
    releases and the next press.
    """
    out: List[str] = []
    for i in range(n):
        if i > 0:
            out.append(f"D{gap_ms}")
        else:
            # Small priming gap even before the first long press, to
            # make sure the student firmware has actually entered its
            # steady-state listening loop.
            out.append(f"D{gap_ms}")
        out.append("L")
    return out


# ---------------------------------------------------------------------------
# Lab 3 segment list (base rubric -- brightness extra-credit not yet
# handled; those segments will be appended by the capture orchestrator
# only for students whose LLM tag says they implemented it).
# ---------------------------------------------------------------------------


SEGMENTS: List[Segment] = [
    # -- 1 -------------------------------------------------------------
    Segment(
        name="baseline",
        description="Normal clock runs with no button input",
        stimulus=[],
        warmup_ms=1500,
        observe_ms=15000,   # long window so analyzer can verify 1 Hz timing
        graded_items=[
            "normal_clock_runs",
            "normal_clock_timing_1hz",
        ],
    ),

    # -- 2 -------------------------------------------------------------
    Segment(
        name="debounce_reject",
        description="2 ms glitch press is rejected by the debouncer",
        stimulus=["G"],
        warmup_ms=1500,
        observe_ms=4000,
        graded_items=["debounce_rejects_glitch"],
    ),

    # -- 3 -------------------------------------------------------------
    Segment(
        name="enter_hour_set",
        description="Single long press enters Hour-Set mode; hour LED flashes",
        stimulus=["L"],
        warmup_ms=1500,
        observe_ms=5000,
        graded_items=[
            "long_enters_hour_set",
            "hour_flashes_in_hour_set",
            "minute_steady_in_hour_set",
            "clock_does_not_advance_in_hour_set",
        ],
    ),

    # -- 4 -------------------------------------------------------------
    Segment(
        name="hour_increment_3",
        description="Enter hour-set, then three short presses increment the hour by 3",
        stimulus=["L", *_shorts(3)],
        warmup_ms=1500,
        observe_ms=3500,
        graded_items=[
            "short_increments_hour",
        ],
    ),

    # -- 5 -------------------------------------------------------------
    # Enter hour-set, then 13 short presses. Starting hour is whatever
    # the firmware boots to (typically 12); 13 presses crosses 12 -> 1
    # at least once, so the wrap is observable regardless of start.
    Segment(
        name="hour_wrap",
        description="Enter hour-set, 13 short presses to observe 12->1 wrap",
        stimulus=["L", *_shorts(13)],
        warmup_ms=1500,
        observe_ms=3500,
        graded_items=["hour_wraps_12_to_1"],
    ),

    # -- 6 -------------------------------------------------------------
    Segment(
        name="enter_minute_set",
        description="Two long presses enter Minute-Set mode; minute LED flashes",
        stimulus=_longs(2),
        warmup_ms=1500,
        observe_ms=5000,
        graded_items=[
            "long_enters_minute_set",
            "minute_flashes_in_minute_set",
            "hour_steady_in_minute_set",
            "clock_does_not_advance_in_minute_set",
        ],
    ),

    # -- 7 -------------------------------------------------------------
    Segment(
        name="minute_increment_4",
        description="Enter minute-set, then four short presses increment minute by 4 LEDs",
        stimulus=[*_longs(2), *_shorts(4)],
        warmup_ms=1500,
        observe_ms=3500,
        graded_items=["short_increments_minute"],
    ),

    # -- 8 -------------------------------------------------------------
    # 13 presses crosses 55->0 (12 LED positions) at least once.
    Segment(
        name="minute_wrap",
        description="Enter minute-set, 13 short presses to observe 55->0 wrap",
        stimulus=[*_longs(2), *_shorts(13)],
        warmup_ms=1500,
        observe_ms=3500,
        graded_items=["minute_wraps_55_to_0"],
    ),

    # -- 9 -------------------------------------------------------------
    Segment(
        name="return_to_normal",
        description="Three long presses cycle Normal -> Hour -> Minute -> Normal; clock resumes",
        stimulus=_longs(3),
        warmup_ms=1500,
        observe_ms=6000,   # give the clock time to tick a couple of seconds
        graded_items=[
            "long_returns_to_normal",
            "clock_advances_after_return",
        ],
    ),
]


# Convenience lookup.
SEGMENTS_BY_NAME = {s.name: s for s in SEGMENTS}


def select_segments(names: Optional[Sequence[str]]) -> List[Segment]:
    """Return the segments matching ``names``, preserving ``SEGMENTS`` order.

    If ``names`` is falsy, returns all segments.
    Raises KeyError if any requested name is unknown.
    """
    if not names:
        return list(SEGMENTS)
    wanted = set(names)
    unknown = wanted - set(SEGMENTS_BY_NAME)
    if unknown:
        raise KeyError(
            f"unknown segment name(s): {sorted(unknown)}; "
            f"valid: {sorted(SEGMENTS_BY_NAME)}"
        )
    return [s for s in SEGMENTS if s.name in wanted]


def estimate_segment_s(
    segment: Segment,
    flash_s: float = 7.0,
    boot_s: float = 1.0,
) -> float:
    """Estimate wall-clock time for one segment, for duration budgeting.

    Components: flash + boot + warmup + stimulus + observe.
    """
    return (
        flash_s
        + boot_s
        + segment.warmup_ms / 1000.0
        + estimate_stimulus_s(segment.stimulus)
        + segment.observe_ms / 1000.0
    )


def estimate_total_s(
    segments: Sequence[Segment],
    settle_s: float = 3.0,
    trailing_s: float = 3.0,
) -> float:
    """Total estimated capture time for the given segment list."""
    return (
        settle_s
        + sum(estimate_segment_s(s) for s in segments)
        + trailing_s
    )
