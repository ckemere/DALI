"""Lab 3 video analyzer — score rubric items from LED timeline data.

Consumes:
  - Timeline from ``VideoAnalyzer.extract_timeline()`` (frame-level
    LED on/off states and brightness values).
  - Host-side metadata from ``<student>.json`` (segment timing,
    per-token stimulus events with millisecond timestamps).

Produces:
  - Dict of ``{rubric_item: {"verdict": "PASS"/"FAIL"/"NO_DATA",
    "detail": str}}`` for every video rubric item.

The analyzer aligns the video timeline with metadata timestamps by
matching debug-LED on-to-off transitions (reflash boundaries) in the
video to the ``flash_end_ms`` field in the metadata.  Within each
segment, per-token event times from the metadata locate the exact
observation windows for each rubric check.
"""

from __future__ import annotations

import json
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple


# =====================================================================
# Constants
# =====================================================================

MIN_FLASH_TRANSITIONS = 3
STEADY_ON_MIN_FRAC = 0.65
MIN_TICKING_CHANGES = 2
N_LEDS = 12

# How long after a press release to wait before sampling (ms).
# Lets the firmware finish its LED update.
SETTLE_MS = 300

# How long of a window to sample after settling (ms).
SAMPLE_WINDOW_MS = 1500


# =====================================================================
# Result helpers
# =====================================================================

def _pass(detail: str = "") -> Dict[str, str]:
    return {"verdict": "PASS", "detail": detail}


def _fail(detail: str = "") -> Dict[str, str]:
    return {"verdict": "FAIL", "detail": detail}


def _no_data(detail: str = "") -> Dict[str, str]:
    return {"verdict": "NO_DATA", "detail": detail}


# =====================================================================
# Detection primitives
# =====================================================================

def _frames_between(
    timeline: Sequence[Dict],
    t_start: float,
    t_end: float,
) -> List[Dict]:
    return [f for f in timeline if t_start <= f["t"] <= t_end]


def _on_fraction(frames: Sequence[Dict], ring: str, idx: int) -> float:
    if not frames:
        return 0.0
    return sum(1 for f in frames if f[ring][idx]) / len(frames)


def _count_transitions(
    frames: Sequence[Dict], ring: str, idx: int,
) -> int:
    if len(frames) < 2:
        return 0
    n = 0
    for i in range(1, len(frames)):
        if frames[i][ring][idx] != frames[i - 1][ring][idx]:
            n += 1
    return n


def _is_flashing(
    frames: Sequence[Dict],
    ring: str,
    idx: int,
    min_trans: int = MIN_FLASH_TRANSITIONS,
) -> bool:
    return _count_transitions(frames, ring, idx) >= min_trans


def _is_steady_on(
    frames: Sequence[Dict],
    ring: str,
    idx: int,
    min_frac: float = STEADY_ON_MIN_FRAC,
) -> bool:
    return _on_fraction(frames, ring, idx) >= min_frac


def _dominant_position(
    frames: Sequence[Dict],
    ring: str,
    n_leds: int = N_LEDS,
) -> Tuple[Optional[int], float]:
    """LED with the highest on-fraction.  Works for both steady and
    flashing LEDs (a 50% duty-cycle flasher still dominates if all
    others are off)."""
    best_idx: Optional[int] = None
    best_frac = 0.0
    for i in range(n_leds):
        frac = _on_fraction(frames, ring, i)
        if frac > best_frac:
            best_frac = frac
            best_idx = i
    return best_idx, best_frac


def _any_flashing(
    frames: Sequence[Dict],
    ring: str,
    n_leds: int = N_LEDS,
    min_trans: int = MIN_FLASH_TRANSITIONS,
) -> Tuple[bool, Optional[int]]:
    """True if *any* LED in ``ring`` is flashing.  Returns the index."""
    for i in range(n_leds):
        if _is_flashing(frames, ring, i, min_trans):
            return True, i
    return False, None


def _detect_ticking(
    frames: Sequence[Dict],
    ring: str,
    n_leds: int = N_LEDS,
) -> Tuple[bool, int, float]:
    """Detect clock ticking via position changes in overlapping 0.5 s
    windows.  Returns ``(is_ticking, n_changes, avg_period_s)``."""
    if len(frames) < 6:
        return False, 0, 0.0

    t_start = frames[0]["t"]
    t_end = frames[-1]["t"]
    if t_end - t_start < 1.0:
        return False, 0, 0.0

    window_s = 0.5
    step_s = 0.25
    positions: List[int] = []
    times: List[float] = []

    t = t_start
    while t + window_s <= t_end + 0.01:
        win = _frames_between(frames, t, t + window_s)
        if win:
            pos, frac = _dominant_position(win, ring, n_leds)
            if pos is not None and frac > 0.1:
                positions.append(pos)
                times.append(t + window_s / 2)
        t += step_s

    change_times: List[float] = []
    for i in range(1, len(positions)):
        if positions[i] != positions[i - 1]:
            change_times.append(times[i])

    n_changes = len(change_times)
    if n_changes < 2:
        return n_changes >= 1, n_changes, 0.0

    periods = [change_times[i] - change_times[i - 1]
               for i in range(1, len(change_times))]
    return True, n_changes, statistics.mean(periods)


def _track_positions_after_presses(
    timeline: Sequence[Dict],
    ring: str,
    press_events: Sequence[Dict],
    vt_fn,
    n_leds: int = N_LEDS,
    settle_ms: int = SETTLE_MS,
    window_ms: int = SAMPLE_WINDOW_MS,
) -> List[Optional[int]]:
    """After each press event, find the dominant LED position.

    ``vt_fn(meta_ms)`` converts a metadata timestamp to video time.
    Returns one position per press (or None if undetectable).
    """
    positions: List[Optional[int]] = []
    for ev in press_events:
        t_start = vt_fn(ev["end_ms"] + settle_ms)
        t_end = t_start + window_ms / 1000.0
        win = _frames_between(timeline, t_start, t_end)
        if not win:
            positions.append(None)
            continue
        pos, frac = _dominant_position(win, ring, n_leds)
        positions.append(pos if frac > 0.05 else None)
    return positions


def _count_increments(positions: List[Optional[int]], n_leds: int = N_LEDS) -> int:
    """Count consecutive +1 (mod n_leds) steps in a position list."""
    good = 0
    for i in range(1, len(positions)):
        if positions[i] is None or positions[i - 1] is None:
            continue
        if (positions[i] - positions[i - 1]) % n_leds == 1:
            good += 1
    return good


def _has_wrap(positions: List[Optional[int]], n_leds: int = N_LEDS) -> bool:
    """True if the sequence contains a wrap from position (n_leds-1) to 0."""
    for i in range(1, len(positions)):
        if positions[i - 1] == n_leds - 1 and positions[i] == 0:
            return True
    return False


# =====================================================================
# Analyzer
# =====================================================================

class Lab3Analyzer:
    """Score Lab 3 rubric items from a video timeline + capture metadata."""

    def __init__(
        self,
        timeline: Sequence[Dict],
        metadata: Dict[str, Any],
        verbose: bool = False,
    ):
        self.timeline = list(timeline)
        self.metadata = metadata
        self.verbose = verbose

        seg_list = metadata.get("segments", [])
        self._seg_meta: Dict[str, Dict] = {
            s["name"]: s for s in seg_list
        }

        self._debug_off_edges = self._find_debug_off_edges()
        self._anchors = self._build_anchors(seg_list)

    # ── alignment ───────────────────────────────────────────────────

    def _find_debug_off_edges(self) -> List[float]:
        """Video times where the debug LED transitions solidly on → off."""
        MIN_ON_FRAMES = 10
        burst_start = None
        burst_count = 0
        edges: List[float] = []

        for f in self.timeline:
            if f["debug"]:
                if burst_start is None:
                    burst_start = f["t"]
                burst_count += 1
            else:
                if burst_count >= MIN_ON_FRAMES:
                    edges.append(f["t"])
                burst_start = None
                burst_count = 0

        if self.verbose:
            print(f"  [analyzer] found {len(edges)} debug-off edge(s)")
        return edges

    def _build_anchors(
        self, seg_list: Sequence[Dict],
    ) -> Dict[str, Tuple[float, int]]:
        """Map segment name → (debug_off_video_t, flash_end_ms).

        Non-reflash segments inherit from the previous reflash segment.
        """
        reflash_segs = [
            s for s in seg_list
            if s.get("reflash", True) and s.get("flash_success")
        ]

        anchors: Dict[str, Tuple[float, int]] = {}
        for edge_t, seg in zip(self._debug_off_edges, reflash_segs):
            anchors[seg["name"]] = (edge_t, seg.get("flash_end_ms", 0))
            if self.verbose:
                print(f"  [analyzer] anchor {seg['name']}: "
                      f"video_t={edge_t:.2f}  flash_end={seg.get('flash_end_ms', 0)}ms")

        prev: Optional[Tuple[float, int]] = None
        for seg in seg_list:
            name = seg["name"]
            if name in anchors:
                prev = anchors[name]
            elif prev is not None:
                anchors[name] = prev
        return anchors

    def _meta_to_video_t(self, seg_name: str, meta_ms: int) -> float:
        """Convert metadata ms (from rec_t0) to video time."""
        edge_t, flash_end_ms = self._anchors[seg_name]
        return edge_t + (meta_ms - flash_end_ms) / 1000.0

    def _vt_fn(self, seg_name: str):
        """Return a closure that converts meta_ms → video_t for a segment."""
        edge_t, flash_end_ms = self._anchors[seg_name]
        return lambda meta_ms: edge_t + (meta_ms - flash_end_ms) / 1000.0

    # ── main entry point ────────────────────────────────────────────

    def analyze(self) -> Dict[str, Dict[str, str]]:
        """Score all video rubric items.  Returns ``{item: {verdict, detail}}``."""
        results: Dict[str, Dict[str, str]] = {}
        results.update(self._score_baseline())
        results.update(self._score_debounce())
        results.update(self._score_short_reject())
        results.update(self._score_full_cycle())
        return results

    # ── segment scorers ─────────────────────────────────────────────

    def _seg_window(
        self, seg_name: str, start_key: str, end_key: str,
    ) -> List[Dict]:
        """Frames between two metadata timestamp keys for a segment."""
        meta = self._seg_meta.get(seg_name)
        if not meta or seg_name not in self._anchors:
            return []
        t_start = self._meta_to_video_t(seg_name, meta.get(start_key, 0))
        t_end = self._meta_to_video_t(seg_name, meta.get(end_key, 0))
        return _frames_between(self.timeline, t_start, t_end)

    def _score_baseline(self) -> Dict[str, Dict[str, str]]:
        meta = self._seg_meta.get("baseline")
        if not meta or "baseline" not in self._anchors:
            return {
                "normal_clock_runs": _no_data("baseline segment missing"),
                "normal_clock_timing_1hz": _no_data("baseline segment missing"),
            }

        frames = self._seg_window("baseline", "warmup_end_ms", "observe_end_ms")
        if len(frames) < 10:
            return {
                "normal_clock_runs": _no_data(f"only {len(frames)} frames"),
                "normal_clock_timing_1hz": _no_data(f"only {len(frames)} frames"),
            }

        # Check that the inner ring is ticking (clock is running).
        ticking, n_changes, avg_period = _detect_ticking(frames, "inner")

        # Check that outer ring has at least one LED active.
        outer_pos, outer_frac = _dominant_position(frames[:30], "outer")
        outer_active = outer_pos is not None and outer_frac > 0.3

        if ticking and outer_active:
            clock_runs = _pass(
                f"inner ticks ({n_changes} changes), "
                f"outer active at pos {outer_pos}")
        elif ticking:
            clock_runs = _pass(
                f"inner ticks ({n_changes} changes), "
                f"outer not clearly active (frac={outer_frac:.2f})")
        else:
            clock_runs = _fail(
                f"inner not ticking ({n_changes} changes in "
                f"{len(frames)} frames)")

        # Timing check.
        if ticking and avg_period > 0:
            if 0.7 <= avg_period <= 1.4:
                timing = _pass(f"period={avg_period:.2f}s")
            else:
                timing = _fail(f"period={avg_period:.2f}s (expected ~1.0)")
        else:
            timing = _no_data("could not measure tick period")

        return {
            "normal_clock_runs": clock_runs,
            "normal_clock_timing_1hz": timing,
        }

    def _score_debounce(self) -> Dict[str, Dict[str, str]]:
        meta = self._seg_meta.get("debounce_reject")
        if not meta or "debounce_reject" not in self._anchors:
            return {"debounce_rejects_glitch": _no_data("segment missing")}

        vt = self._vt_fn("debounce_reject")

        stim_events = meta.get("stim_events", [])
        glitch = [e for e in stim_events if e["token"] == "G"]
        if not glitch:
            # Fallback: use warmup_end / stim_end from metadata.
            t_before_end = self._meta_to_video_t(
                "debounce_reject", meta.get("warmup_end_ms", 0))
            t_after_start = self._meta_to_video_t(
                "debounce_reject", meta.get("stim_end_ms", 0))
        else:
            t_before_end = vt(glitch[0]["start_ms"])
            t_after_start = vt(glitch[0]["end_ms"] + SETTLE_MS)

        t_after_end = self._meta_to_video_t(
            "debounce_reject", meta.get("observe_end_ms", 0))

        # Sample ~1 s before glitch.
        before = _frames_between(
            self.timeline, t_before_end - 1.0, t_before_end)
        after = _frames_between(
            self.timeline, t_after_start, min(t_after_start + 2.0, t_after_end))

        if len(before) < 5 or len(after) < 5:
            return {"debounce_rejects_glitch": _no_data(
                f"before={len(before)} after={len(after)} frames")}

        # Compare outer and inner dominant positions before/after.
        ob, _ = _dominant_position(before, "outer")
        oa, _ = _dominant_position(after, "outer")
        ib, _ = _dominant_position(before, "inner")
        ia, _ = _dominant_position(after, "inner")

        # Also check that no flashing started (mode change).
        flash_after, _ = _any_flashing(after, "outer")

        if ob == oa and not flash_after:
            return {"debounce_rejects_glitch": _pass(
                f"outer {ob}→{oa}, inner {ib}→{ia}, no flash")}
        else:
            detail = f"outer {ob}→{oa}, inner {ib}→{ia}"
            if flash_after:
                detail += ", flashing detected (mode change?)"
            return {"debounce_rejects_glitch": _fail(detail)}

    def _score_short_reject(self) -> Dict[str, Dict[str, str]]:
        meta = self._seg_meta.get("short_press_reject")
        if not meta or "short_press_reject" not in self._anchors:
            return {"short_press_ignored_in_normal": _no_data("segment missing")}

        vt = self._vt_fn("short_press_reject")

        stim_events = meta.get("stim_events", [])
        shorts = [e for e in stim_events if e["token"] == "S"]

        if not shorts:
            t_before_end = self._meta_to_video_t(
                "short_press_reject", meta.get("warmup_end_ms", 0))
            t_after_start = self._meta_to_video_t(
                "short_press_reject", meta.get("stim_end_ms", 0))
        else:
            t_before_end = vt(shorts[0]["start_ms"])
            t_after_start = vt(shorts[0]["end_ms"] + SETTLE_MS)

        t_after_end = self._meta_to_video_t(
            "short_press_reject", meta.get("observe_end_ms", 0))

        before = _frames_between(
            self.timeline, t_before_end - 1.5, t_before_end)
        after = _frames_between(
            self.timeline, t_after_start, min(t_after_start + 2.0, t_after_end))

        if len(before) < 5 or len(after) < 5:
            return {"short_press_ignored_in_normal": _no_data(
                f"before={len(before)} after={len(after)} frames")}

        # No flashing should start (no mode change).
        flash_outer, _ = _any_flashing(after, "outer")
        flash_inner, _ = _any_flashing(after, "inner")

        # Clock should still be ticking normally.
        ticking, _, _ = _detect_ticking(after, "inner")

        if not flash_outer and not flash_inner:
            return {"short_press_ignored_in_normal": _pass(
                f"no mode change, ticking={ticking}")}
        else:
            return {"short_press_ignored_in_normal": _fail(
                f"flashing detected: outer={flash_outer} inner={flash_inner}")}

    # ── full cycle scoring ──────────────────────────────────────────

    def _score_full_cycle(self) -> Dict[str, Dict[str, str]]:
        """Score all rubric items from the full_cycle segment."""
        meta = self._seg_meta.get("full_cycle")
        if not meta or "full_cycle" not in self._anchors:
            return self._full_cycle_no_data("segment missing")

        stim_events = meta.get("stim_events")
        if not stim_events:
            return self._full_cycle_no_data("no stim_events in metadata")

        vt = self._vt_fn("full_cycle")

        longs = [e for e in stim_events if e["token"] == "L"]
        shorts = [e for e in stim_events if e["token"] == "S"]

        if len(longs) < 4 or len(shorts) < 39:
            return self._full_cycle_no_data(
                f"expected 4L+39S, got {len(longs)}L+{len(shorts)}S")

        hour_shorts = shorts[0:13]
        minute_shorts = shorts[13:26]
        brightness_shorts = shorts[26:39]

        observe_end = self._meta_to_video_t(
            "full_cycle", meta.get("observe_end_ms", 0))

        results: Dict[str, Dict[str, str]] = {}

        # ── Hour-Set phase ──────────────────────────────────────────

        # Window after 1st long press, before 1st short press.
        hs_entry = _frames_between(
            self.timeline,
            vt(longs[0]["end_ms"] + SETTLE_MS),
            vt(hour_shorts[0]["start_ms"]),
        )

        if len(hs_entry) < 5:
            results["long_enters_hour_set"] = _no_data(
                f"entry window too short ({len(hs_entry)} frames)")
        else:
            outer_flash, outer_flash_idx = _any_flashing(hs_entry, "outer")
            inner_pos, inner_frac = _dominant_position(hs_entry, "inner")
            inner_steady = (
                inner_pos is not None
                and _is_steady_on(hs_entry, "inner", inner_pos)
            )

            if outer_flash:
                results["long_enters_hour_set"] = _pass(
                    f"outer[{outer_flash_idx}] flashing")
            else:
                results["long_enters_hour_set"] = _fail(
                    "no outer LED flashing after long press")

            if outer_flash:
                trans = _count_transitions(hs_entry, "outer", outer_flash_idx)
                results["hour_flashes_in_hour_set"] = _pass(
                    f"outer[{outer_flash_idx}] {trans} transitions")
            else:
                results["hour_flashes_in_hour_set"] = _fail(
                    "no flashing detected")

            if inner_steady:
                results["minute_steady_in_hour_set"] = _pass(
                    f"inner[{inner_pos}] on_frac={_on_fraction(hs_entry, 'inner', inner_pos):.2f}")
            else:
                inner_flash, _ = _any_flashing(hs_entry, "inner")
                results["minute_steady_in_hour_set"] = _fail(
                    f"inner flashing={inner_flash}, "
                    f"on_frac={_on_fraction(hs_entry, 'inner', inner_pos) if inner_pos is not None else 0:.2f}")

        # Clock frozen: inner ring position should not change during
        # the entire hour-set phase (1st long → 2nd long).
        hs_full = _frames_between(
            self.timeline,
            vt(longs[0]["end_ms"] + SETTLE_MS),
            vt(longs[1]["start_ms"]),
        )
        if len(hs_full) < 10:
            results["clock_does_not_advance_in_hour_set"] = _no_data(
                f"only {len(hs_full)} frames")
        else:
            ticking, n_chg, _ = _detect_ticking(hs_full, "inner")
            if not ticking or n_chg == 0:
                results["clock_does_not_advance_in_hour_set"] = _pass(
                    f"inner stable ({n_chg} changes)")
            else:
                results["clock_does_not_advance_in_hour_set"] = _fail(
                    f"inner changed {n_chg} times")

        # Hour increment tracking.
        hour_positions = _track_positions_after_presses(
            self.timeline, "outer", hour_shorts, vt)
        good_inc = _count_increments(hour_positions)

        if good_inc >= 10:
            results["short_increments_hour"] = _pass(
                f"{good_inc}/12 correct +1 steps; "
                f"positions={hour_positions}")
        else:
            results["short_increments_hour"] = _fail(
                f"only {good_inc}/12 correct +1 steps; "
                f"positions={hour_positions}")

        if _has_wrap(hour_positions):
            results["hour_wraps_12_to_1"] = _pass(
                f"wrap 11→0 seen in {hour_positions}")
        else:
            results["hour_wraps_12_to_1"] = _fail(
                f"no 11→0 wrap in {hour_positions}")

        # ── Minute-Set phase ────────────────────────────────────────

        ms_entry = _frames_between(
            self.timeline,
            vt(longs[1]["end_ms"] + SETTLE_MS),
            vt(minute_shorts[0]["start_ms"]),
        )

        if len(ms_entry) < 5:
            results["long_enters_minute_set"] = _no_data(
                f"entry window too short ({len(ms_entry)} frames)")
        else:
            inner_flash, inner_flash_idx = _any_flashing(ms_entry, "inner")
            outer_pos, outer_frac = _dominant_position(ms_entry, "outer")
            outer_steady = (
                outer_pos is not None
                and _is_steady_on(ms_entry, "outer", outer_pos)
            )

            if inner_flash:
                results["long_enters_minute_set"] = _pass(
                    f"inner[{inner_flash_idx}] flashing")
            else:
                results["long_enters_minute_set"] = _fail(
                    "no inner LED flashing after 2nd long press")

            if inner_flash:
                trans = _count_transitions(ms_entry, "inner", inner_flash_idx)
                results["minute_flashes_in_minute_set"] = _pass(
                    f"inner[{inner_flash_idx}] {trans} transitions")
            else:
                results["minute_flashes_in_minute_set"] = _fail(
                    "no flashing detected")

            if outer_steady:
                results["hour_steady_in_minute_set"] = _pass(
                    f"outer[{outer_pos}] on_frac={_on_fraction(ms_entry, 'outer', outer_pos):.2f}")
            else:
                outer_flash_chk, _ = _any_flashing(ms_entry, "outer")
                results["hour_steady_in_minute_set"] = _fail(
                    f"outer flashing={outer_flash_chk}")

        # Clock frozen: outer ring position stable during minute-set.
        ms_full = _frames_between(
            self.timeline,
            vt(longs[1]["end_ms"] + SETTLE_MS),
            vt(longs[2]["start_ms"]),
        )
        if len(ms_full) < 10:
            results["clock_does_not_advance_in_minute_set"] = _no_data(
                f"only {len(ms_full)} frames")
        else:
            ticking, n_chg, _ = _detect_ticking(ms_full, "outer")
            if not ticking or n_chg == 0:
                results["clock_does_not_advance_in_minute_set"] = _pass(
                    f"outer stable ({n_chg} changes)")
            else:
                results["clock_does_not_advance_in_minute_set"] = _fail(
                    f"outer changed {n_chg} times")

        # Minute increment tracking.
        minute_positions = _track_positions_after_presses(
            self.timeline, "inner", minute_shorts, vt)
        good_inc = _count_increments(minute_positions)

        if good_inc >= 10:
            results["short_increments_minute"] = _pass(
                f"{good_inc}/12 correct +1 steps; "
                f"positions={minute_positions}")
        else:
            results["short_increments_minute"] = _fail(
                f"only {good_inc}/12 correct +1 steps; "
                f"positions={minute_positions}")

        if _has_wrap(minute_positions):
            results["minute_wraps_55_to_0"] = _pass(
                f"wrap 11→0 seen in {minute_positions}")
        else:
            results["minute_wraps_55_to_0"] = _fail(
                f"no 11→0 wrap in {minute_positions}")

        # ── Return to Normal (non-EC: after 3rd long) ───────────────

        post_3rd = _frames_between(
            self.timeline,
            vt(longs[2]["end_ms"] + SETTLE_MS),
            vt(brightness_shorts[0]["start_ms"]),
        )

        # For non-EC students, the 3rd long returns to Normal mode.
        # We check for ticking (not flashing) in the gap before the
        # (ignored) short presses.
        if len(post_3rd) < 5:
            results["long_returns_to_normal"] = _no_data(
                f"window too short ({len(post_3rd)} frames)")
            results["clock_advances_after_return"] = _no_data("window too short")
        else:
            outer_flash_3, _ = _any_flashing(post_3rd, "outer")
            inner_flash_3, _ = _any_flashing(post_3rd, "inner")
            neither_flashing = not outer_flash_3 and not inner_flash_3

            if neither_flashing:
                results["long_returns_to_normal"] = _pass(
                    "no flashing after 3rd long press")
            else:
                results["long_returns_to_normal"] = _fail(
                    f"flashing: outer={outer_flash_3} inner={inner_flash_3}")

            # Also check ticking across the wider post-3rd-long window
            # (the 13 "ignored" short presses with 2 s gaps = ~26 s).
            wide_post_3rd = _frames_between(
                self.timeline,
                vt(longs[2]["end_ms"] + SETTLE_MS),
                vt(longs[3]["start_ms"]),
            )
            ticking_3, n_chg_3, period_3 = _detect_ticking(
                wide_post_3rd, "inner")
            if ticking_3 and n_chg_3 >= MIN_TICKING_CHANGES:
                results["clock_advances_after_return"] = _pass(
                    f"{n_chg_3} inner changes, period={period_3:.2f}s")
            elif neither_flashing:
                results["clock_advances_after_return"] = _fail(
                    f"normal mode but only {n_chg_3} inner changes")
            else:
                results["clock_advances_after_return"] = _fail(
                    "still in set mode (flashing)")

        # ── EC Brightness-Set (after 3rd long) ──────────────────────

        # For EC students, the 3rd long enters Brightness-Set mode.
        # Both rings should flash.
        if len(post_3rd) < 5:
            results["long_enters_brightness_set"] = _no_data("window too short")
            results["both_flash_in_brightness_set"] = _no_data("window too short")
        else:
            both_flash = (
                _any_flashing(post_3rd, "outer")[0]
                and _any_flashing(post_3rd, "inner")[0]
            )
            if both_flash:
                results["long_enters_brightness_set"] = _pass(
                    "both rings flashing after 3rd long")
                results["both_flash_in_brightness_set"] = _pass(
                    "confirmed")
            else:
                results["long_enters_brightness_set"] = _fail(
                    "both rings not flashing after 3rd long")
                results["both_flash_in_brightness_set"] = _fail(
                    f"outer_flash={_any_flashing(post_3rd, 'outer')[0]} "
                    f"inner_flash={_any_flashing(post_3rd, 'inner')[0]}")

        # Brightness responds to short presses: compare average
        # brightness across the first few press intervals.
        bri_readings: List[float] = []
        for ev in brightness_shorts[:5]:
            t_s = vt(ev["end_ms"] + SETTLE_MS)
            t_e = t_s + SAMPLE_WINDOW_MS / 1000.0
            win = _frames_between(self.timeline, t_s, t_e)
            if win:
                # Mean brightness across all outer + inner LEDs.
                all_bri = []
                for f in win:
                    all_bri.extend(f.get("outer_brightness", []))
                    all_bri.extend(f.get("inner_brightness", []))
                if all_bri:
                    bri_readings.append(statistics.mean(all_bri))

        if len(bri_readings) >= 3:
            changes = sum(
                1 for i in range(1, len(bri_readings))
                if abs(bri_readings[i] - bri_readings[i - 1]) > 3.0
            )
            if changes >= 2:
                results["brightness_responds_to_short"] = _pass(
                    f"{changes} brightness changes in {len(bri_readings)} readings")
            else:
                results["brightness_responds_to_short"] = _fail(
                    f"only {changes} brightness changes: {[f'{b:.0f}' for b in bri_readings]}")
        else:
            results["brightness_responds_to_short"] = _no_data(
                f"only {len(bri_readings)} brightness readings")

        # ── EC Return to Normal (after 4th long) ────────────────────

        post_4th = _frames_between(
            self.timeline,
            vt(longs[3]["end_ms"] + SETTLE_MS),
            observe_end,
        )

        if len(post_4th) < 10:
            results["long_returns_to_normal_ec"] = _no_data(
                f"window too short ({len(post_4th)} frames)")
            results["clock_advances_after_return_ec"] = _no_data(
                "window too short")
        else:
            outer_flash_4, _ = _any_flashing(post_4th, "outer")
            inner_flash_4, _ = _any_flashing(post_4th, "inner")

            if not outer_flash_4 and not inner_flash_4:
                results["long_returns_to_normal_ec"] = _pass(
                    "no flashing after 4th long press")
            else:
                results["long_returns_to_normal_ec"] = _fail(
                    f"flashing: outer={outer_flash_4} inner={inner_flash_4}")

            ticking_4, n_chg_4, period_4 = _detect_ticking(
                post_4th, "inner")
            if ticking_4 and n_chg_4 >= MIN_TICKING_CHANGES:
                results["clock_advances_after_return_ec"] = _pass(
                    f"{n_chg_4} inner changes, period={period_4:.2f}s")
            else:
                results["clock_advances_after_return_ec"] = _fail(
                    f"only {n_chg_4} inner changes")

        return results

    @staticmethod
    def _full_cycle_no_data(reason: str) -> Dict[str, Dict[str, str]]:
        items = [
            "long_enters_hour_set",
            "hour_flashes_in_hour_set",
            "minute_steady_in_hour_set",
            "clock_does_not_advance_in_hour_set",
            "short_increments_hour",
            "hour_wraps_12_to_1",
            "long_enters_minute_set",
            "minute_flashes_in_minute_set",
            "hour_steady_in_minute_set",
            "clock_does_not_advance_in_minute_set",
            "short_increments_minute",
            "minute_wraps_55_to_0",
            "long_returns_to_normal",
            "clock_advances_after_return",
            "long_enters_brightness_set",
            "both_flash_in_brightness_set",
            "brightness_responds_to_short",
            "long_returns_to_normal_ec",
            "clock_advances_after_return_ec",
        ]
        return {item: _no_data(reason) for item in items}


# =====================================================================
# Convenience: analyze from file paths
# =====================================================================

def analyze_from_files(
    video_path: str,
    calibration_path: str,
    metadata_path: str,
    sample_fps: int = 0,
    verbose: bool = False,
) -> Dict[str, Dict[str, str]]:
    """One-call entry point: load video + calibration + metadata, analyze.

    Returns the same dict as ``Lab3Analyzer.analyze()``.
    """
    from assess.video import VideoAnalyzer

    va = VideoAnalyzer(calibration_path)
    timeline = va.extract_timeline(video_path, sample_fps=sample_fps,
                                   verbose=verbose)

    with open(metadata_path) as f:
        metadata = json.load(f)

    analyzer = Lab3Analyzer(timeline, metadata, verbose=verbose)
    return analyzer.analyze()
