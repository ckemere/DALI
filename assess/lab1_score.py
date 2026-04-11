"""
Lab 1 rubric scoring from video timeline data.

Consumes the timeline produced by assess.video.VideoAnalyzer
and checks Lab 1 specific rubric items (LED timing, clockwise
sequence, wrapping, hour increment, etc.).
"""

try:
    import numpy as np
except ImportError:
    np = None

# Rubric score keys that appear in the results dict.
SCORE_FIELDS = [
    "t0_offset",
    "leds_activated",
    "avg_leds_on",
    "pct_exactly_2_on",
    "distinct_rings",
    "all_24_leds_seen",
    "timing_1hz",
    "timing_interval",
    "inner_clockwise_sequence",
    "outer_clockwise_sequence",
    "inner_sequence_wrap",
    "outer_sequence_wrap",
    "full_clock_cycle",
    "hour_increment_at_wrap",
    "total_state_changes",
]

# Video-based rubric items that carry point values.
# These are the SCORE_FIELDS entries that produce PASS/FAIL verdicts.
VIDEO_RUBRIC_ITEMS = [
    "distinct_rings",
    "all_24_leds_seen",
    "timing_1hz",
    "inner_clockwise_sequence",
    "outer_clockwise_sequence",
    "inner_sequence_wrap",
    "outer_sequence_wrap",
    "full_clock_cycle",
    "hour_increment_at_wrap",
]

VIDEO_RUBRIC_POINTS = {
    "distinct_rings":            1,
    "all_24_leds_seen":          1,
    "timing_1hz":                1,
    "inner_clockwise_sequence":  1,
    "outer_clockwise_sequence":  1,
    "inner_sequence_wrap":       1,
    "outer_sequence_wrap":       1,
    "full_clock_cycle":          1,
    "hour_increment_at_wrap":    1,
}

VIDEO_RUBRIC_DESCRIPTIONS = {
    "distinct_rings":            "Both LED rings active",
    "all_24_leds_seen":          "All 24 LEDs activated",
    "timing_1hz":                "Timing ~1 Hz",
    "inner_clockwise_sequence":  "Inner ring steps clockwise",
    "outer_clockwise_sequence":  "Outer ring steps clockwise",
    "inner_sequence_wrap":       "Inner ring wraps (11\u21920)",
    "outer_sequence_wrap":       "Outer ring wraps (11\u21920)",
    "full_clock_cycle":          "Complete 12-hour clock cycle",
    "hour_increment_at_wrap":    "Hour advances on second wrap",
}

VIDEO_RUBRIC_MAX_POINTS = sum(VIDEO_RUBRIC_POINTS.get(k, 1)
                              for k in VIDEO_RUBRIC_ITEMS)


def video_verdict(field, raw_value):
    """Extract a PASS/FAIL verdict from a raw video score field value."""
    if not raw_value or raw_value == "NO_DATA":
        return "NO_DATA"
    val = str(raw_value).upper()
    if val.startswith("PASS"):
        return "PASS"
    if val.startswith("FAIL"):
        return "FAIL"
    if val.startswith("PARTIAL"):
        return "PARTIAL"
    if val.startswith("NOT_OBSERVED"):
        return "NOT_OBSERVED"
    return "UNCLEAR"


def _extract_single_led_sequence(frames, ring_key):
    """
    From a list of frames, extract the sequence of active LED indices
    for a given ring ('outer' or 'inner'), keeping only frames where
    exactly one LED is on in that ring.  Consecutive duplicates are
    removed so we get the sequence of transitions.

    Returns a list of (time, led_index) tuples.
    """
    seq = []
    for s in frames:
        active = [i for i in range(12) if s[ring_key][i]]
        if len(active) == 1:
            idx = active[0]
            if not seq or seq[-1][1] != idx:
                seq.append((s["t"], idx))
    return seq


def _check_clockwise(seq):
    """
    Given a deduplicated sequence of (time, led_index), check whether
    transitions are clockwise (each step is +1 mod 12).

    Returns (verdict_str, n_correct, n_total).
    """
    if len(seq) < 2:
        return "NO_DATA", 0, 0

    correct = 0
    total = len(seq) - 1
    for i in range(total):
        expected_next = (seq[i][1] + 1) % 12
        if seq[i + 1][1] == expected_next:
            correct += 1

    pct = correct / total * 100
    if pct >= 80:
        verdict = "PASS"
    elif pct >= 50:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"
    return f"{verdict} ({correct}/{total} steps clockwise, {pct:.0f}%)", correct, total


def _check_wrap(seq):
    """
    Check whether the sequence contains a wrap (LED 11 -> LED 0).
    """
    for i in range(len(seq) - 1):
        if seq[i][1] == 11 and seq[i + 1][1] == 0:
            return True
    return False


def score(timeline):
    """
    Score video-detectable rubric items for Lab 1.

    Args:
        timeline: list of dicts from VideoAnalyzer.extract_timeline()

    Returns:
        (results, changes, initial_outer, initial_inner)
        results       : dict mapping score field names to result strings
        changes       : list of dicts describing every state change
                        [{"t": float, "outer_on": [int], "outer_off": [int],
                          "inner_on": [int], "inner_off": [int]}, ...]
        initial_outer : [bool]*12 LED state at t=0
        initial_inner : [bool]*12 LED state at t=0
    """
    if not timeline:
        empty = {k: "NO_DATA" for k in SCORE_FIELDS}
        return empty, [], [], []

    results = {}

    # Filter to only post-programming frames (t >= 0)
    post_flash = [s for s in timeline if s["t"] >= 0]
    if not post_flash:
        empty = {k: "NO_DATA" for k in SCORE_FIELDS}
        return empty, [], [], []

    results["t0_offset"] = f"{timeline[0]['t']:.1f}s"

    # ── All 24 LEDs activated at some point ───────────────────────
    outer_seen = [False] * 12
    inner_seen = [False] * 12
    for s in post_flash:
        for i in range(12):
            if s["outer"][i]:
                outer_seen[i] = True
            if s["inner"][i]:
                inner_seen[i] = True
    o_count = sum(outer_seen)
    i_count = sum(inner_seen)
    results["leds_activated"] = (
        f"{o_count + i_count}/24 "
        f"(outer:{o_count}/12, inner:{i_count}/12)"
    )

    if len(post_flash) < 2:
        for k in SCORE_FIELDS:
            results.setdefault(k, "NO_DATA")
        return results, [], [], []

    # ── LED count per frame (expect 2) ────────────────────────────
    # Allow a one-frame grace period for transitions: if more than
    # 2 LEDs are on, but every extra LED was already on in the
    # previous frame (i.e. it's still turning off), count the frame
    # as acceptable.
    counts = [sum(s["outer"]) + sum(s["inner"]) for s in post_flash]
    results["avg_leds_on"] = f"{np.mean(counts):.1f}"
    ok_frames = 0
    for i, s in enumerate(post_flash):
        n = counts[i]
        if n <= 2:
            ok_frames += 1
        elif i > 0:
            prev = post_flash[i - 1]
            cur_outer = set(j for j in range(12) if s["outer"][j])
            cur_inner = set(j for j in range(12) if s["inner"][j])
            prev_outer = set(j for j in range(12) if prev["outer"][j])
            prev_inner = set(j for j in range(12) if prev["inner"][j])
            new_outer = cur_outer - prev_outer
            new_inner = cur_inner - prev_inner
            new_count = len(new_outer) + len(new_inner)
            if new_count <= 1:
                ok_frames += 1
    pct_two = ok_frames / len(post_flash) * 100
    results["pct_exactly_2_on"] = f"{pct_two:.0f}%"

    # ── Distinct Rings ────────────────────────────────────────────
    if any(outer_seen) and any(inner_seen):
        results["distinct_rings"] = "PASS"
    elif any(outer_seen) or any(inner_seen):
        results["distinct_rings"] = "PARTIAL"
    else:
        results["distinct_rings"] = "FAIL"

    # ── Timing ~1 Hz (based on inner ring changes) ────────────────
    change_times = []
    for i in range(1, len(post_flash)):
        if post_flash[i]["inner"] != post_flash[i - 1]["inner"]:
            change_times.append(post_flash[i]["t"])

    if len(change_times) >= 3:
        intervals = np.diff(change_times)
        # Drop very short intervals (sampling artefacts)
        intervals = intervals[intervals > 0.3]
        if len(intervals) > 0:
            avg = float(np.mean(intervals))
            std = float(np.std(intervals))
            results["timing_interval"] = f"{avg:.2f}s (std={std:.2f}s)"
            results["timing_1hz"] = (
                "PASS" if 0.7 <= avg <= 1.5 else "FAIL"
            )
        else:
            results["timing_1hz"] = "FAIL (no consistent changes)"
            results["timing_interval"] = ""
    else:
        results["timing_1hz"] = "FAIL (too few changes)"
        results["timing_interval"] = ""

    # ── Clockwise Sequence (inner and outer rings) ────────────────
    inner_seq = _extract_single_led_sequence(post_flash, "inner")
    outer_seq = _extract_single_led_sequence(post_flash, "outer")

    inner_cw, _, _ = _check_clockwise(inner_seq)
    outer_cw, _, _ = _check_clockwise(outer_seq)
    results["inner_clockwise_sequence"] = inner_cw
    results["outer_clockwise_sequence"] = outer_cw

    # ── Sequence Wrap (inner and outer, separately) ───────────────
    results["inner_sequence_wrap"] = (
        "PASS" if _check_wrap(inner_seq) else "NOT_OBSERVED"
    )
    results["outer_sequence_wrap"] = (
        "PASS" if _check_wrap(outer_seq) else "NOT_OBSERVED"
    )

    # ── All 24 LEDs seen (verdict) ─────────────────────────────────
    if o_count == 12 and i_count == 12:
        results["all_24_leds_seen"] = "PASS (24/24)"
    else:
        results["all_24_leds_seen"] = (
            f"FAIL ({o_count + i_count}/24, "
            f"outer:{o_count}/12, inner:{i_count}/12)"
        )

    # ── Full 12-hour cycle ─────────────────────────────────────────
    # Check that the inner (second) hand visited all 12 positions
    # in a complete 0→1→2→…→11→0 cycle, AND that the outer (hour)
    # hand advanced through all 12 positions over the full video.
    inner_positions_seen = set(idx for _, idx in inner_seq)
    outer_positions_seen = set(idx for _, idx in outer_seq)
    inner_wraps = sum(
        1 for i in range(len(inner_seq) - 1)
        if inner_seq[i][1] == 11 and inner_seq[i + 1][1] == 0
    )
    outer_wraps = sum(
        1 for i in range(len(outer_seq) - 1)
        if outer_seq[i][1] == 11 and outer_seq[i + 1][1] == 0
    )

    inner_full = len(inner_positions_seen) == 12 and inner_wraps >= 1
    outer_full = len(outer_positions_seen) == 12 and outer_wraps >= 1
    if inner_full and outer_full:
        results["full_clock_cycle"] = (
            f"PASS (inner: 12/12 pos, {inner_wraps} wrap(s); "
            f"outer: 12/12 pos, {outer_wraps} wrap(s))"
        )
    else:
        parts = []
        parts.append(f"inner: {len(inner_positions_seen)}/12 pos, "
                      f"{inner_wraps} wrap(s)")
        parts.append(f"outer: {len(outer_positions_seen)}/12 pos, "
                      f"{outer_wraps} wrap(s)")
        results["full_clock_cycle"] = f"FAIL ({'; '.join(parts)})"

    # ── Hour Increment at Wrap ────────────────────────────────────
    # The hour hand (outer) should advance once each time the second
    # hand (inner) completes a full 12-tick cycle, driven by the same
    # firmware interrupt.  We verify this in a *calibration-invariant*
    # way — comparing timing, not LED indices — because the calibration
    # click order determines where "index 0" falls on the physical
    # ring, and an off-by-one calibration should not silently break
    # this rubric item.
    #
    # Two conditions must hold for a working clock:
    #   1. Rate:      #inner_transitions ≈ 12 × #outer_transitions
    #                 (the outer advances once every 12 inner ticks)
    #   2. Alignment: every outer transition is temporally aligned
    #                 with an inner transition (same IRQ → same frame
    #                 at any reasonable frame rate).
    #
    # Direction of the outer hand is already graded separately by
    # `outer_clockwise_sequence`, so this check does NOT also require
    # a +1 index step.
    n_inner = max(len(inner_seq) - 1, 0)
    n_outer = max(len(outer_seq) - 1, 0)

    if n_inner >= 12 and n_outer >= 1:
        inner_tick_times = [t for t, _ in inner_seq[1:]]

        # Condition 2: each outer tick aligned with an inner tick.
        ALIGN_TOL = 0.6  # seconds — accommodates 5 fps sampling jitter
        aligned = 0
        for j in range(n_outer):
            t_outer = outer_seq[j + 1][0]
            if any(abs(t_outer - t_inner) <= ALIGN_TOL
                   for t_inner in inner_tick_times):
                aligned += 1
        align_pct = aligned / n_outer * 100

        # Condition 1: rate ≈ 12:1.  Allow some slack to tolerate the
        # video starting mid-cycle or ending mid-cycle.
        ratio = n_inner / n_outer
        rate_ok = 10.0 <= ratio <= 14.0

        if rate_ok and align_pct >= 80:
            results["hour_increment_at_wrap"] = (
                f"PASS ({aligned}/{n_outer} outer ticks aligned with "
                f"inner, ratio={ratio:.1f}:1)"
            )
        elif align_pct >= 80 or rate_ok:
            results["hour_increment_at_wrap"] = (
                f"PARTIAL ({aligned}/{n_outer} aligned, ratio={ratio:.1f}:1)"
            )
        else:
            results["hour_increment_at_wrap"] = (
                f"FAIL ({aligned}/{n_outer} aligned, ratio={ratio:.1f}:1)"
            )
    elif n_inner < 12:
        results["hour_increment_at_wrap"] = (
            "NOT_OBSERVED (not enough inner ticks observed)")
    else:
        results["hour_increment_at_wrap"] = (
            "NOT_OBSERVED (outer ring did not advance)")

    # ── Change log (useful for manual timing review) ──────────────
    all_changes = []
    for i in range(1, len(post_flash)):
        prev, cur = post_flash[i - 1], post_flash[i]
        if cur["outer"] != prev["outer"] or cur["inner"] != prev["inner"]:
            outer_on = [
                j for j in range(12)
                if cur["outer"][j] and not prev["outer"][j]
            ]
            outer_off = [
                j for j in range(12)
                if not cur["outer"][j] and prev["outer"][j]
            ]
            inner_on = [
                j for j in range(12)
                if cur["inner"][j] and not prev["inner"][j]
            ]
            inner_off = [
                j for j in range(12)
                if not cur["inner"][j] and prev["inner"][j]
            ]
            all_changes.append({
                "t": round(cur["t"], 2),
                "outer_on": outer_on,
                "outer_off": outer_off,
                "inner_on": inner_on,
                "inner_off": inner_off,
            })

    results["total_state_changes"] = str(len(all_changes))

    # Include the initial LED state so callers can replay
    initial_outer = list(post_flash[0]["outer"])
    initial_inner = list(post_flash[0]["inner"])

    return results, all_changes, initial_outer, initial_inner
