#!/usr/bin/env python3
"""
Video analysis for Lab 1 grading.

Reads a recorded video of the LED clock board, extracts per-frame LED
states using a calibration file, and scores the video-detectable rubric
items.

Standalone usage (for testing):
    python analyze_lab1_video.py <video.mp4> <calibration.json>
"""

import json
import os
import sys

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

# Rubric score keys that appear in the results dict.
SCORE_FIELDS = [
    "t0_offset",
    "leds_start_off",
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


class VideoAnalyzer:
    """Analyze LED board video using calibration data."""

    def __init__(self, calibration_path):
        if cv2 is None:
            raise ImportError(
                "opencv-python and numpy required: "
                "pip install opencv-python numpy"
            )
        with open(calibration_path) as f:
            cal = json.load(f)
        self.outer_pos = [(p["x"], p["y"]) for p in cal["outer_ring"]]
        self.inner_pos = [(p["x"], p["y"]) for p in cal["inner_ring"]]
        # Debug/programming LED (optional for backward compat)
        debug = cal.get("debug_led", [])
        self.debug_pos = (debug[0]["x"], debug[0]["y"]) if debug else None
        self.radius = cal.get("sample_radius", 15)
        self.threshold = cal.get("threshold", 128)

    # ── internal helpers ────────────────────────────────────────────

    def _brightness(self, gray, x, y):
        """Mean brightness in a square patch around (x, y)."""
        r = self.radius
        h, w = gray.shape
        y1, y2 = max(0, y - r), min(h, y + r)
        x1, x2 = max(0, x - r), min(w, x + r)
        roi = gray[y1:y2, x1:x2]
        if roi.size == 0:
            return 0.0
        return float(np.mean(roi))

    # ── timeline extraction ─────────────────────────────────────────

    def _detect_t0(self, debug_samples):
        """
        Find the moment programming ends by looking for the debug LED
        to stop flickering.  During flash the LED toggles rapidly;
        once the student code starts it typically goes steady or off.

        Args:
            debug_samples: list of (time, is_on) tuples, sorted by time.

        Returns:
            t0 in seconds, or 0.0 if detection fails.
        """
        if len(debug_samples) < 10:
            return 0.0

        # Slide a 1-second window and count transitions (on↔off).
        # Flash activity = high transition count.  We want the last
        # window that had significant flickering.
        window_sec = 1.0
        best_end = 0.0
        i_start = 0

        for i_end in range(1, len(debug_samples)):
            t_end = debug_samples[i_end][0]
            # advance window start
            while (debug_samples[i_start][0] < t_end - window_sec
                   and i_start < i_end):
                i_start += 1
            # count transitions in window
            transitions = sum(
                1 for j in range(i_start + 1, i_end + 1)
                if debug_samples[j][1] != debug_samples[j - 1][1]
            )
            if transitions >= 3:
                best_end = t_end

        return best_end

    def extract_timeline(self, video_path, sample_fps=5, verbose=False):
        """
        Sample LED on/off states from a video file.

        If a debug LED position is calibrated, detects the end of
        programming (flash activity) and reports times relative to
        that point (t=0 = code starts running).

        Args:
            video_path: Path to the .mp4 file.
            sample_fps: How many samples per second to take (default 5).
                        Higher = more timing precision, slower to process.
            verbose:    Print brightness diagnostics for the first few
                        frames so you can verify the threshold.

        Returns:
            List of dicts: [{"t": float, "outer": [bool]*12,
                             "inner": [bool]*12, "debug": bool}, ...]
            where t is seconds since code started running (negative
            values are during programming).
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        skip = max(1, int(fps / sample_fps))

        raw = []
        idx = 0
        diag_count = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if idx % skip == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                t = idx / fps
                outer_bri = [
                    self._brightness(gray, x, y)
                    for x, y in self.outer_pos
                ]
                inner_bri = [
                    self._brightness(gray, x, y)
                    for x, y in self.inner_pos
                ]
                debug_bri = (
                    self._brightness(gray, *self.debug_pos)
                    if self.debug_pos else 0.0
                )
                outer = [b > self.threshold for b in outer_bri]
                inner = [b > self.threshold for b in inner_bri]
                debug = debug_bri > self.threshold

                if verbose and diag_count < 5:
                    all_bri = outer_bri + inner_bri
                    print(f"  [diag] t={t:.2f}s  threshold={self.threshold}  "
                          f"debug={debug_bri:.0f}  "
                          f"LED min={min(all_bri):.0f}  max={max(all_bri):.0f}  "
                          f"mean={np.mean(all_bri):.0f}  "
                          f"on={sum(outer)+sum(inner)}/24")
                    diag_count += 1

                raw.append({
                    "t": t, "outer": outer, "inner": inner, "debug": debug,
                })
            idx += 1

        cap.release()

        if verbose and raw:
            # Sample a frame from the middle of the video too
            mid = raw[len(raw) // 2]
            print(f"  [diag] mid-video t={mid['t']:.2f}s  "
                  f"on={sum(mid['outer'])+sum(mid['inner'])}/24")

        # Detect t0 from debug LED flicker
        t0 = 0.0
        if self.debug_pos and raw:
            debug_samples = [(s["t"], s["debug"]) for s in raw]
            t0 = self._detect_t0(debug_samples)

        # Shift all times so t0 = 0
        for s in raw:
            s["t"] = round(s["t"] - t0, 3)

        return raw

    # ── rubric scoring ──────────────────────────────────────────────

    def score(self, timeline):
        """
        Score video-detectable rubric items.

        Returns:
            (results, changes)
            results : dict mapping score field names to result strings
            changes : list of dicts describing every state change
                      [{"t": float, "outer_changed": [int],
                        "inner_changed": [int]}, ...]
        """
        if not timeline:
            empty = {k: "NO_DATA" for k in SCORE_FIELDS}
            return empty, []

        results = {}

        # Filter to only post-programming frames (t >= 0)
        post_flash = [s for s in timeline if s["t"] >= 0]
        if not post_flash:
            empty = {k: "NO_DATA" for k in SCORE_FIELDS}
            return empty, []

        results["t0_offset"] = f"{timeline[0]['t']:.1f}s"
        total_time = post_flash[-1]["t"]

        # ── #9  LEDs start OFF ──────────────────────────────────────
        # Check first 2 seconds after code starts running (t=0..2)
        early = [s for s in post_flash if s["t"] < 2.0]
        if early:
            all_off = all(
                not any(s["outer"]) and not any(s["inner"]) for s in early
            )
            results["leds_start_off"] = "PASS" if all_off else "FAIL"
        else:
            results["leds_start_off"] = "NO_DATA"

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
            return results, []

        # ── #11  LED count per frame (expect 2) ─────────────────────
        counts = [sum(s["outer"]) + sum(s["inner"]) for s in running]
        results["avg_leds_on"] = f"{np.mean(counts):.1f}"
        pct_two = sum(1 for c in counts if c == 2) / len(counts) * 100
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
                outer_diff = [
                    j for j in range(12)
                    if cur["outer"][j] != prev["outer"][j]
                ]
                inner_diff = [
                    j for j in range(12)
                    if cur["inner"][j] != prev["inner"][j]
                ]
                all_changes.append({
                    "t": round(cur["t"], 2),
                    "outer_changed": outer_diff,
                    "inner_changed": inner_diff,
                })

        results["total_state_changes"] = str(len(all_changes))

        return results, all_changes


# ── standalone CLI ──────────────────────────────────────────────────

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <video.mp4> <calibration.json>")
        sys.exit(1)

    video_path, cal_path = sys.argv[1], sys.argv[2]

    analyzer = VideoAnalyzer(cal_path)
    print(f"Analyzing: {video_path}")
    print(f"Threshold: {analyzer.threshold}  Sample radius: {analyzer.radius}")
    timeline = analyzer.extract_timeline(video_path, verbose=True)

    if not timeline:
        print("No frames extracted.")
        sys.exit(1)

    print(f"Extracted {len(timeline)} samples over {timeline[-1]['t']:.1f}s\n")

    results, changes = analyzer.score(timeline)

    print("=== Rubric Scores ===")
    for key in SCORE_FIELDS:
        val = results.get(key, "")
        if val:
            print(f"  {key:25s} {val}")

    print(f"\n=== State Changes (first 30 of {len(changes)}) ===")
    for c in changes[:30]:
        print(
            f"  t={c['t']:7.2f}s  "
            f"outer:{c['outer_changed']}  inner:{c['inner_changed']}"
        )
    if len(changes) > 30:
        print(f"  ... and {len(changes) - 30} more")


if __name__ == "__main__":
    main()
