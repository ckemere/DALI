"""Lab 3 test segment definitions and stimulus runner.

A *segment* is one stimulus-plus-observe block in the Lab 3 grading
capture.  During a single continuous recording we execute multiple
segments, recording everything.  Most segments start with a fresh
reflash (``reflash=True``, the default) so the board is in a known
initial state.  Some segments continue from the previous one
(``reflash=False``) to avoid unnecessary reflash overhead.

The video analyzer later slices the recording at the debug-LED
transitions and scores each segment against ``graded_items``.

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
from typing import Dict, List, Optional, Sequence

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
    """One stimulus + observe block in the Lab 3 capture.

    Attributes:
        name:         Short, file-safe identifier used in logs / CSV.
        description:  Human-readable sentence for reports.
        stimulus:     Sequence of tokens in the mini-DSL above.
        graded_items: Rubric item keys this segment is meant to validate.
                      Scoring wires up a segment's observe window to
                      these keys later. Purely informational for capture.
        warmup_ms:    Quiet time after boot (or previous segment) and
                      before first stimulus token.
        observe_ms:   Quiet time after the last stimulus token.
        reflash:      If True (default), reflash the board before this
                      segment so it starts from a known initial state.
                      If False, continue from whatever state the
                      previous segment left the board in.
    """

    name: str
    description: str
    stimulus: List[str]
    graded_items: List[str]
    warmup_ms: int = 1500
    observe_ms: int = 3000
    reflash: bool = True


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
    ref_t0: float = 0.0,
) -> List[Dict[str, object]]:
    """Execute the stimulus DSL against ``helper``.

    Blocks until every token completes.  Returns a list of per-token
    event dicts::

        [{"token": "L", "start_ms": 1234, "end_ms": 2789}, ...]

    Times are in ms relative to ``ref_t0``.  Pass
    ``ref_t0=time.monotonic()`` at recording start so event times
    align with other metadata timestamps.

    Raises whatever the helper raises on protocol errors -- the
    capture orchestrator catches it and logs the segment as failed.
    """
    events: List[Dict[str, object]] = []
    for tok in tokens:
        t_start = time.monotonic()
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
        t_end = time.monotonic()
        events.append({
            "token": tok,
            "start_ms": int((t_start - ref_t0) * 1000),
            "end_ms": int((t_end - ref_t0) * 1000),
        })
    return events


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
# Lab 3 segment list
#
# Four segments, three reflashes.  Segments 1-2 share one flash (the
# debounce test continues from the running clock).  Segment 3 and 4
# each get a fresh flash.
#
# Segment 4 runs the entire FSM cycle in one shot: Hour-Set ->
# Minute-Set -> Brightness/Normal -> return.  The analyzer uses the
# host-side timing log to identify sub-phases within the long stimulus
# and score each rubric item at the right point in the sequence.
# ---------------------------------------------------------------------------


SEGMENTS: List[Segment] = [
    # -- 1: baseline clock operation -----------------------------------
    # Watch the clock tick for 25+ seconds: enough time for the inner
    # ring to complete a full revolution (12 ticks) even if the student
    # is ticking every 2 seconds, plus see the hour hand advance once.
    Segment(
        name="baseline",
        description="Normal clock runs with no button input; full revolution + hour increment",
        stimulus=[],
        warmup_ms=1500,
        observe_ms=25000,
        graded_items=[
            "normal_clock_runs",
            "normal_clock_timing_1hz",
        ],
    ),

    # -- 2: debounce rejection (no reflash) ----------------------------
    # Continues from the running clock.  A 2 ms glitch should not cause
    # a mode change or LED position change.
    Segment(
        name="debounce_reject",
        description="2 ms glitch press is rejected by the debouncer",
        stimulus=["G"],
        warmup_ms=0,
        observe_ms=4000,
        reflash=False,
        graded_items=["debounce_rejects_glitch"],
    ),

    # -- 3: short press rejection (fresh flash) ------------------------
    # A short press in Normal mode should be ignored — no mode change.
    Segment(
        name="short_press_reject",
        description="Short press in Normal mode is ignored (no mode change)",
        stimulus=["D3000", "S"],
        warmup_ms=1500,
        observe_ms=4000,
        graded_items=["short_press_ignored_in_normal"],
    ),

    # -- 4: full FSM cycle (fresh flash) -------------------------------
    # Tests the entire mode cycle in one continuous sequence:
    #   L  -> enter Hour-Set
    #   13 S (2 s gaps) -> cycle hour hand all the way around + 1
    #   L  -> enter Minute-Set
    #   13 S (2 s gaps) -> cycle minute hand all the way around + 1
    #   L  -> enter Brightness-Set (EC) or return to Normal (non-EC)
    #   13 S (2 s gaps) -> change brightness (EC) or ignored (non-EC)
    #   L  -> return to Normal (EC) or enter Hour-Set (non-EC)
    #   observe 8 s   -> watch for ticking
    #
    # For non-EC students, return-to-normal is checked between the 3rd
    # long press and the subsequent (ignored) short presses — the 2 s
    # gap gives ~60 frames to see clock ticking.  The 4th long press
    # then enters Hour-Set, so the final observe shows flashing.
    #
    # For EC students, the 4th long press returns to Normal, so the
    # final observe shows ticking.
    Segment(
        name="full_cycle",
        description="Full mode cycle: hour-set, minute-set, brightness/normal, and return",
        stimulus=[
            "L",                        # 1st long: enter Hour-Set
            *_shorts(13, gap_ms=2000),  # 13 short presses, 2 s apart
            "D2000", "L",               # 2nd long: enter Minute-Set
            *_shorts(13, gap_ms=2000),  # 13 short presses, 2 s apart
            "D2000", "L",               # 3rd long: Brightness (EC) or Normal
            *_shorts(13, gap_ms=2000),  # 13 short presses, 2 s apart
            "D2000", "L",               # 4th long: Normal (EC) or Hour-Set
        ],
        warmup_ms=3000,
        observe_ms=8000,
        graded_items=[
            # Hour-Set phase
            "long_enters_hour_set",
            "hour_flashes_in_hour_set",
            "minute_steady_in_hour_set",
            "clock_does_not_advance_in_hour_set",
            "short_increments_hour",
            "hour_wraps_12_to_1",
            # Minute-Set phase
            "long_enters_minute_set",
            "minute_flashes_in_minute_set",
            "hour_steady_in_minute_set",
            "clock_does_not_advance_in_minute_set",
            "short_increments_minute",
            "minute_wraps_55_to_0",
            # Return to normal (non-EC: between 3rd long and shorts)
            "long_returns_to_normal",
            "clock_advances_after_return",
            # Brightness EC phase
            "long_enters_brightness_set",
            "both_flash_in_brightness_set",
            "brightness_responds_to_short",
            # Return to normal (EC: after 4th long)
            "long_returns_to_normal_ec",
            "clock_advances_after_return_ec",
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

    Components: [flash + boot] + warmup + stimulus + observe.
    Flash and boot are skipped when ``segment.reflash`` is False.
    """
    t = segment.warmup_ms / 1000.0
    t += estimate_stimulus_s(segment.stimulus)
    t += segment.observe_ms / 1000.0
    if segment.reflash:
        t += flash_s + boot_s
    return t


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
