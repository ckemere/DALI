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
# Detection primitives  (dual-mode: brightness or threshold)
#
# When ``ub=True`` (use_brightness), primitives operate on raw pixel
# brightness values from ``outer_brightness`` / ``inner_brightness``
# fields — immune to threshold miscalibration.
#
# When ``ub=False``, primitives use the boolean ``outer`` / ``inner``
# fields (on-fraction, toggle counting) — the original threshold-based
# approach which works well when calibration thresholds are correct.
#
# ``brightness_responds_to_short`` always uses brightness regardless of
# mode, since it inherently measures a continuous quantity.
# =====================================================================

_BRI_KEY = {"outer": "outer_brightness", "inner": "inner_brightness"}

MIN_ACTIVE_BRIGHTNESS = 20
MIN_FLASH_RANGE = 30

# Threshold-mode: LED considered "on" when on-fraction >= this.
_ON_FRAC_THRESHOLD = 0.65


def _frames_between(
    timeline: Sequence[Dict],
    t_start: float,
    t_end: float,
) -> List[Dict]:
    return [f for f in timeline if t_start <= f["t"] <= t_end]


def _mean_brightness(
    frames: Sequence[Dict], ring: str, idx: int,
) -> float:
    if not frames:
        return 0.0
    bri_key = _BRI_KEY[ring]
    return sum(f[bri_key][idx] for f in frames) / len(frames)


def _count_transitions(
    frames: Sequence[Dict], ring: str, idx: int,
    ub: bool = True,
) -> int:
    """Count transitions in the LED time-series.

    ``ub=True``: midpoint crossings in brightness.
    ``ub=False``: boolean toggles (True→False / False→True).
    """
    if len(frames) < 2:
        return 0

    if ub:
        bri_key = _BRI_KEY[ring]
        values = [f[bri_key][idx] for f in frames]
        lo, hi = min(values), max(values)
        if hi - lo < MIN_FLASH_RANGE:
            return 0
        mid = (lo + hi) / 2.0
        n = 0
        above = values[0] > mid
        for v in values[1:]:
            now_above = v > mid
            if now_above != above:
                n += 1
                above = now_above
        return n
    else:
        vals = [f[ring][idx] for f in frames]
        n = 0
        for i in range(1, len(vals)):
            if vals[i] != vals[i - 1]:
                n += 1
        return n


def _is_flashing(
    frames: Sequence[Dict],
    ring: str,
    idx: int,
    min_trans: int = MIN_FLASH_TRANSITIONS,
    ub: bool = True,
) -> bool:
    if len(frames) < 4:
        return False

    if ub:
        bri_key = _BRI_KEY[ring]
        values = [f[bri_key][idx] for f in frames]
        if max(values) < MIN_ACTIVE_BRIGHTNESS:
            return False
    else:
        on_frac = sum(1 for f in frames if f[ring][idx]) / len(frames)
        if on_frac < 0.1:
            return False

    return _count_transitions(frames, ring, idx, ub=ub) >= min_trans


def _is_steady_on(
    frames: Sequence[Dict],
    ring: str,
    idx: int,
    ub: bool = True,
) -> bool:
    if ub:
        mean_bri = _mean_brightness(frames, ring, idx)
        if mean_bri < MIN_ACTIVE_BRIGHTNESS:
            return False
    else:
        on_frac = sum(1 for f in frames if f[ring][idx]) / len(frames)
        if on_frac < _ON_FRAC_THRESHOLD:
            return False
    return not _is_flashing(frames, ring, idx, ub=ub)


def _dominant_position(
    frames: Sequence[Dict],
    ring: str,
    n_leds: int = N_LEDS,
    ub: bool = True,
) -> Tuple[Optional[int], float]:
    """LED with the highest activity.

    ``ub=True``: highest mean brightness. Returns ``(index, mean_bri)``.
    ``ub=False``: highest on-fraction. Returns ``(index, on_fraction)``.
    """
    if not frames:
        return None, 0.0

    if ub:
        bri_key = _BRI_KEY[ring]
        best_idx: Optional[int] = None
        best_val = -1.0
        for i in range(n_leds):
            m = sum(f[bri_key][i] for f in frames) / len(frames)
            if m > best_val:
                best_val = m
                best_idx = i
        if best_val < MIN_ACTIVE_BRIGHTNESS:
            return None, best_val
        return best_idx, best_val
    else:
        best_idx = None
        best_val = -1.0
        for i in range(n_leds):
            frac = sum(1 for f in frames if f[ring][i]) / len(frames)
            if frac > best_val:
                best_val = frac
                best_idx = i
        if best_val < _ON_FRAC_THRESHOLD:
            return None, best_val
        return best_idx, best_val


def _any_flashing(
    frames: Sequence[Dict],
    ring: str,
    n_leds: int = N_LEDS,
    min_trans: int = MIN_FLASH_TRANSITIONS,
    ub: bool = True,
) -> Tuple[bool, Optional[int]]:
    for i in range(n_leds):
        if _is_flashing(frames, ring, i, min_trans, ub=ub):
            return True, i
    return False, None


def _detect_ticking(
    frames: Sequence[Dict],
    ring: str,
    n_leds: int = N_LEDS,
    ub: bool = True,
) -> Tuple[bool, int, float]:
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
            pos, _val = _dominant_position(win, ring, n_leds, ub=ub)
            if pos is not None:
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
    ub: bool = True,
) -> List[Optional[int]]:
    """After each press event, find the dominant LED position."""
    positions: List[Optional[int]] = []
    for ev in press_events:
        t_start = vt_fn(ev["end_ms"] + settle_ms)
        t_end = t_start + window_ms / 1000.0
        win = _frames_between(timeline, t_start, t_end)
        if not win:
            positions.append(None)
            continue
        pos, _val = _dominant_position(win, ring, n_leds, ub=ub)
        positions.append(pos)
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
        use_brightness: bool = True,
    ):
        self.timeline = list(timeline)
        self.metadata = metadata
        self.verbose = verbose
        self._ub = use_brightness

        seg_list = metadata.get("segments", [])
        self._seg_meta: Dict[str, Dict] = {
            s["name"]: s for s in seg_list
        }

        self._debug_off_edges = self._find_debug_off_edges()
        self._anchors = self._build_anchors(seg_list)

    # ── alignment ───────────────────────────────────────────────────

    def _find_debug_off_edges(self) -> List[float]:
        """Video times where the debug LED finishes a programming cycle.

        The XDS110 debug LED may blink multiple times during a single
        flash (not a solid on), so we collect individual on→off edges
        and then merge nearby ones (within ``MERGE_GAP_S``) into one
        event per flash.  The merged edge time is the *last* off-edge
        in each cluster.
        """
        MIN_ON_FRAMES = 10
        MERGE_GAP_S = 3.0

        # Step 1: collect raw on→off edges (filtering short glitches).
        raw_edges: List[float] = []
        burst_count = 0
        for f in self.timeline:
            if f["debug"]:
                burst_count += 1
            else:
                if burst_count >= MIN_ON_FRAMES:
                    raw_edges.append(f["t"])
                burst_count = 0

        if self.verbose:
            print(f"  [analyzer] raw debug-off edges: {len(raw_edges)}  "
                  f"times={[f'{t:.2f}' for t in raw_edges]}")

        # Step 2: merge edges that are close together (same flash).
        if not raw_edges:
            return []
        merged: List[float] = []
        cluster_end = raw_edges[0]
        for i in range(1, len(raw_edges)):
            if raw_edges[i] - cluster_end > MERGE_GAP_S:
                merged.append(cluster_end)
            cluster_end = raw_edges[i]
        merged.append(cluster_end)

        if self.verbose:
            print(f"  [analyzer] merged debug-off edges: {len(merged)}  "
                  f"times={[f'{t:.2f}' for t in merged]}")
        return merged

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

        ub = self._ub

        ticking, n_changes, avg_period = _detect_ticking(frames, "inner", ub=ub)

        outer_pos, outer_bri = _dominant_position(frames[:30], "outer", ub=ub)
        outer_active = outer_pos is not None

        if ticking and outer_active:
            clock_runs = _pass(
                f"inner ticks ({n_changes} changes), "
                f"outer active at pos {outer_pos} (bri={outer_bri:.0f})")
        elif ticking:
            clock_runs = _pass(
                f"inner ticks ({n_changes} changes), "
                f"outer not clearly active (bri={outer_bri:.0f})")
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

        ub = self._ub

        ob, _ = _dominant_position(before, "outer", ub=ub)
        oa, _ = _dominant_position(after, "outer", ub=ub)
        ib, _ = _dominant_position(before, "inner", ub=ub)
        ia, _ = _dominant_position(after, "inner", ub=ub)

        flash_after, _ = _any_flashing(after, "outer", ub=ub)

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

        ub = self._ub

        flash_outer, _ = _any_flashing(after, "outer", ub=ub)
        flash_inner, _ = _any_flashing(after, "inner", ub=ub)

        ticking, _, _ = _detect_ticking(after, "inner", ub=ub)

        if not flash_outer and not flash_inner:
            return {"short_press_ignored_in_normal": _pass(
                f"no mode change, ticking={ticking}")}
        else:
            return {"short_press_ignored_in_normal": _fail(
                f"flashing detected: outer={flash_outer} inner={flash_inner}")}

    # ── sync-LED press detection (fallback) ───────────────────────

    def _detect_presses_from_sync(
        self, t_start: float, t_end: float,
    ) -> Tuple[List[Dict], List[Dict]]:
        """Detect button presses from sync LED on/off transitions.

        Returns ``(longs, shorts)`` where each entry has
        ``start_ms`` / ``end_ms`` in *video-time* milliseconds.
        """
        LONG_THRESHOLD_S = 0.8
        MIN_PRESS_S = 0.05

        frames = _frames_between(self.timeline, t_start, t_end)
        if not frames:
            return [], []

        presses: List[Dict] = []
        in_press = False
        press_start_t = 0.0

        for f in frames:
            if f.get("sync", False) and not in_press:
                in_press = True
                press_start_t = f["t"]
            elif not f.get("sync", False) and in_press:
                in_press = False
                duration = f["t"] - press_start_t
                if duration >= MIN_PRESS_S:
                    tok = "L" if duration > LONG_THRESHOLD_S else "S"
                    presses.append({
                        "token": tok,
                        "start_ms": int(press_start_t * 1000),
                        "end_ms": int(f["t"] * 1000),
                    })

        longs = [p for p in presses if p["token"] == "L"]
        shorts = [p for p in presses if p["token"] == "S"]

        if self.verbose:
            print(f"  [analyzer] sync LED detected {len(longs)}L + "
                  f"{len(shorts)}S in [{t_start:.1f}, {t_end:.1f}]s")
        return longs, shorts

    # ── full cycle scoring ──────────────────────────────────────────

    def _score_full_cycle(self) -> Dict[str, Dict[str, str]]:
        """Score all rubric items from the full_cycle segment."""
        meta = self._seg_meta.get("full_cycle")
        if not meta or "full_cycle" not in self._anchors:
            return self._full_cycle_no_data("segment missing")

        ub = self._ub

        stim_events = meta.get("stim_events")
        if stim_events:
            vt = self._vt_fn("full_cycle")
            longs = [e for e in stim_events if e["token"] == "L"]
            shorts = [e for e in stim_events if e["token"] == "S"]
        else:
            seg_start = self._meta_to_video_t(
                "full_cycle", meta.get("warmup_end_ms", 0))
            seg_end = self._meta_to_video_t(
                "full_cycle", meta.get("observe_end_ms", 0))
            longs, shorts = self._detect_presses_from_sync(
                seg_start, seg_end)
            vt = lambda ms: ms / 1000.0  # noqa: E731

        if len(longs) < 4 or len(shorts) < 39:
            return self._full_cycle_no_data(
                f"expected 4L+39S, got {len(longs)}L+{len(shorts)}S"
                + (" (from sync LED)" if not stim_events else ""))

        hour_shorts = shorts[0:13]
        minute_shorts = shorts[13:26]
        brightness_shorts = shorts[26:39]

        observe_end = self._meta_to_video_t(
            "full_cycle", meta.get("observe_end_ms", 0))

        results: Dict[str, Dict[str, str]] = {}

        # ── Hour-Set phase ──────────────────────────────────────────

        hs_entry = _frames_between(
            self.timeline,
            vt(longs[0]["end_ms"] + SETTLE_MS),
            vt(hour_shorts[0]["start_ms"]),
        )

        if len(hs_entry) < 5:
            results["long_enters_hour_set"] = _no_data(
                f"entry window too short ({len(hs_entry)} frames)")
        else:
            outer_flash, outer_flash_idx = _any_flashing(hs_entry, "outer", ub=ub)
            inner_pos, inner_frac = _dominant_position(hs_entry, "inner", ub=ub)
            inner_steady = (
                inner_pos is not None
                and _is_steady_on(hs_entry, "inner", inner_pos, ub=ub)
            )

            if outer_flash:
                results["long_enters_hour_set"] = _pass(
                    f"outer[{outer_flash_idx}] flashing")
            else:
                results["long_enters_hour_set"] = _fail(
                    "no outer LED flashing after long press")

            if outer_flash:
                trans = _count_transitions(hs_entry, "outer", outer_flash_idx, ub=ub)
                results["hour_flashes_in_hour_set"] = _pass(
                    f"outer[{outer_flash_idx}] {trans} transitions")
            else:
                results["hour_flashes_in_hour_set"] = _fail(
                    "no flashing detected")

            if inner_steady:
                results["minute_steady_in_hour_set"] = _pass(
                    f"inner[{inner_pos}] bri={_mean_brightness(hs_entry, 'inner', inner_pos):.0f}")
            else:
                inner_flash, _ = _any_flashing(hs_entry, "inner", ub=ub)
                bri = _mean_brightness(hs_entry, 'inner', inner_pos) if inner_pos is not None else 0
                results["minute_steady_in_hour_set"] = _fail(
                    f"inner flashing={inner_flash}, bri={bri:.0f}")

        hs_full = _frames_between(
            self.timeline,
            vt(longs[0]["end_ms"] + SETTLE_MS),
            vt(longs[1]["start_ms"]),
        )
        if len(hs_full) < 10:
            results["clock_does_not_advance_in_hour_set"] = _no_data(
                f"only {len(hs_full)} frames")
        else:
            ticking, n_chg, _ = _detect_ticking(hs_full, "inner", ub=ub)
            if not ticking or n_chg == 0:
                results["clock_does_not_advance_in_hour_set"] = _pass(
                    f"inner stable ({n_chg} changes)")
            else:
                results["clock_does_not_advance_in_hour_set"] = _fail(
                    f"inner changed {n_chg} times")

        hour_positions = _track_positions_after_presses(
            self.timeline, "outer", hour_shorts, vt, ub=ub)
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
            inner_flash, inner_flash_idx = _any_flashing(ms_entry, "inner", ub=ub)
            outer_pos, outer_bri = _dominant_position(ms_entry, "outer", ub=ub)
            outer_steady = (
                outer_pos is not None
                and _is_steady_on(ms_entry, "outer", outer_pos, ub=ub)
            )

            if inner_flash:
                results["long_enters_minute_set"] = _pass(
                    f"inner[{inner_flash_idx}] flashing")
            else:
                results["long_enters_minute_set"] = _fail(
                    "no inner LED flashing after 2nd long press")

            if inner_flash:
                trans = _count_transitions(ms_entry, "inner", inner_flash_idx, ub=ub)
                results["minute_flashes_in_minute_set"] = _pass(
                    f"inner[{inner_flash_idx}] {trans} transitions")
            else:
                results["minute_flashes_in_minute_set"] = _fail(
                    "no flashing detected")

            if outer_steady:
                results["hour_steady_in_minute_set"] = _pass(
                    f"outer[{outer_pos}] bri={_mean_brightness(ms_entry, 'outer', outer_pos):.0f}")
            else:
                outer_flash_chk, _ = _any_flashing(ms_entry, "outer", ub=ub)
                results["hour_steady_in_minute_set"] = _fail(
                    f"outer flashing={outer_flash_chk}")

        ms_full = _frames_between(
            self.timeline,
            vt(longs[1]["end_ms"] + SETTLE_MS),
            vt(longs[2]["start_ms"]),
        )
        if len(ms_full) < 10:
            results["clock_does_not_advance_in_minute_set"] = _no_data(
                f"only {len(ms_full)} frames")
        else:
            ticking, n_chg, _ = _detect_ticking(ms_full, "outer", ub=ub)
            if not ticking or n_chg == 0:
                results["clock_does_not_advance_in_minute_set"] = _pass(
                    f"outer stable ({n_chg} changes)")
            else:
                results["clock_does_not_advance_in_minute_set"] = _fail(
                    f"outer changed {n_chg} times")

        minute_positions = _track_positions_after_presses(
            self.timeline, "inner", minute_shorts, vt, ub=ub)
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

        if len(post_3rd) < 5:
            results["long_returns_to_normal"] = _no_data(
                f"window too short ({len(post_3rd)} frames)")
            results["clock_advances_after_return"] = _no_data("window too short")
        else:
            outer_flash_3, _ = _any_flashing(post_3rd, "outer", ub=ub)
            inner_flash_3, _ = _any_flashing(post_3rd, "inner", ub=ub)
            neither_flashing = not outer_flash_3 and not inner_flash_3

            if neither_flashing:
                results["long_returns_to_normal"] = _pass(
                    "no flashing after 3rd long press")
            else:
                results["long_returns_to_normal"] = _fail(
                    f"flashing: outer={outer_flash_3} inner={inner_flash_3}")

            wide_post_3rd = _frames_between(
                self.timeline,
                vt(longs[2]["end_ms"] + SETTLE_MS),
                vt(longs[3]["start_ms"]),
            )
            ticking_3, n_chg_3, period_3 = _detect_ticking(
                wide_post_3rd, "inner", ub=ub)
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

        if len(post_3rd) < 5:
            results["long_enters_brightness_set"] = _no_data("window too short")
            results["both_flash_in_brightness_set"] = _no_data("window too short")
        else:
            both_flash = (
                _any_flashing(post_3rd, "outer", ub=ub)[0]
                and _any_flashing(post_3rd, "inner", ub=ub)[0]
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
                    f"outer_flash={_any_flashing(post_3rd, 'outer', ub=ub)[0]} "
                    f"inner_flash={_any_flashing(post_3rd, 'inner', ub=ub)[0]}")

        # Brightness responds to short presses: use max per-frame
        # brightness of active LEDs (always brightness-based).
        bri_readings: List[float] = []
        for ev in brightness_shorts[:5]:
            t_s = vt(ev["end_ms"] + SETTLE_MS)
            t_e = t_s + SAMPLE_WINDOW_MS / 1000.0
            win = _frames_between(self.timeline, t_s, t_e)
            if win:
                frame_maxes = []
                for f in win:
                    ob = f.get("outer_brightness", [])
                    ib = f.get("inner_brightness", [])
                    all_bri = list(ob) + list(ib)
                    if all_bri:
                        frame_maxes.append(max(all_bri))
                if frame_maxes:
                    bri_readings.append(statistics.mean(frame_maxes))

        if len(bri_readings) >= 3:
            changes = sum(
                1 for i in range(1, len(bri_readings))
                if abs(bri_readings[i] - bri_readings[i - 1]) > 3.0
            )
            if changes >= 2:
                results["brightness_responds_to_short"] = _pass(
                    f"{changes} brightness changes in {len(bri_readings)} readings: "
                    f"{[f'{b:.0f}' for b in bri_readings]}")
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
            outer_flash_4, _ = _any_flashing(post_4th, "outer", ub=ub)
            inner_flash_4, _ = _any_flashing(post_4th, "inner", ub=ub)

            if not outer_flash_4 and not inner_flash_4:
                results["long_returns_to_normal_ec"] = _pass(
                    "no flashing after 4th long press")
            else:
                results["long_returns_to_normal_ec"] = _fail(
                    f"flashing: outer={outer_flash_4} inner={inner_flash_4}")

            ticking_4, n_chg_4, period_4 = _detect_ticking(
                post_4th, "inner", ub=ub)
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
    use_brightness: bool = True,
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

    analyzer = Lab3Analyzer(timeline, metadata, verbose=verbose,
                            use_brightness=use_brightness)
    return analyzer.analyze()
