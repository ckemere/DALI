"""
Lab 1 rubric scoring from video timeline data.

Consumes the timeline produced by grading.video_analyzer.VideoAnalyzer
and checks Lab 1 specific rubric items.
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
    "infinite_loop",
    "two_hands",
    "distinct_rings",
    "timing_1hz",
    "timing_interval",
    "sequence_wrap",
    "total_state_changes",
]


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
    total_time = post_flash[-1]["t"]

    # ── #10  All 24 LEDs activated at some point ────────────────
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

    # For remaining checks, skip first 3 s after code starts.
    running = [s for s in post_flash if s["t"] > 3.0]
    if not running:
        for k in SCORE_FIELDS:
            results.setdefault(k, "NO_DATA")
        return results, [], [], []

    # ── #11  LED count per frame (expect 2) ─────────────────────
    # Allow a one-frame grace period for transitions: if more than
    # 2 LEDs are on, but every extra LED was already on in the
    # previous frame (i.e. it's still turning off), count the frame
    # as acceptable.
    counts = [sum(s["outer"]) + sum(s["inner"]) for s in running]
    results["avg_leds_on"] = f"{np.mean(counts):.1f}"
    ok_frames = 0
    for i, s in enumerate(running):
        n = counts[i]
        if n <= 2:
            ok_frames += 1
        elif i > 0:
            prev = running[i - 1]
            # The extra LEDs beyond 2 must all have been on in the
            # previous frame (camera caught them mid-turn-off)
            cur_outer = set(j for j in range(12) if s["outer"][j])
            cur_inner = set(j for j in range(12) if s["inner"][j])
            prev_outer = set(j for j in range(12) if prev["outer"][j])
            prev_inner = set(j for j in range(12) if prev["inner"][j])
            new_outer = cur_outer - prev_outer
            new_inner = cur_inner - prev_inner
            new_count = len(new_outer) + len(new_inner)
            if new_count <= 1:
                # At most one genuinely new LED; the rest are lingering
                ok_frames += 1
    pct_two = ok_frames / len(running) * 100
    results["pct_exactly_2_on"] = f"{pct_two:.0f}%"

    # ── #12  Infinite loop (still running in last 30 s) ─────────
    late = [s for s in post_flash if s["t"] > total_time - 30]
    if len(late) > 1:
        changes_late = sum(
            1
            for i in range(1, len(late))
            if (late[i]["outer"] != late[i - 1]["outer"]
                or late[i]["inner"] != late[i - 1]["inner"])
        )
        results["infinite_loop"] = (
            "PASS" if changes_late > 0 else "FAIL"
        )
    else:
        results["infinite_loop"] = "NO_DATA"

    # ── #14  Two Active Hands ───────────────────────────────────
    both = sum(
        1 for s in running if any(s["outer"]) and any(s["inner"])
    )
    both_pct = both / len(running) * 100
    verdict = "PASS" if both_pct > 50 else "FAIL"
    results["two_hands"] = f"{verdict} ({both_pct:.0f}%)"

    # ── #15  Distinct Rings ─────────────────────────────────────
    if any(outer_seen) and any(inner_seen):
        results["distinct_rings"] = "PASS"
    elif any(outer_seen) or any(inner_seen):
        results["distinct_rings"] = "PARTIAL"
    else:
        results["distinct_rings"] = "FAIL"

    # ── #16  Timing ~1 Hz ───────────────────────────────────────
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

    # ── #17  Sequence Wrap ──────────────────────────────────────
    inner_seq = []
    for s in running:
        active = [i for i in range(12) if s["inner"][i]]
        if len(active) == 1:
            inner_seq.append(active[0])

    # De-duplicate consecutive identical values
    deduped = []
    for v in inner_seq:
        if not deduped or deduped[-1] != v:
            deduped.append(v)

    has_wrap = any(
        deduped[i] == 11 and deduped[i + 1] == 0
        for i in range(len(deduped) - 1)
    )
    results["sequence_wrap"] = "PASS" if has_wrap else "NOT_OBSERVED"

    # ── Change log (useful for manual timing review) ────────────
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
