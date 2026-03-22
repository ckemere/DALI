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

    def extract_timeline(self, video_path, sample_fps=5):
        """
        Sample LED on/off states from a video file.

        Args:
            video_path: Path to the .mp4 file.
            sample_fps: How many samples per second to take (default 5).
                        Higher = more timing precision, slower to process.

        Returns:
            List of dicts: [{"t": float, "outer": [bool]*12,
                             "inner": [bool]*12}, ...]
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        skip = max(1, int(fps / sample_fps))

        timeline = []
        idx = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if idx % skip == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                t = idx / fps
                outer = [
                    self._brightness(gray, x, y) > self.threshold
                    for x, y in self.outer_pos
                ]
                inner = [
                    self._brightness(gray, x, y) > self.threshold
                    for x, y in self.inner_pos
                ]
                timeline.append({"t": t, "outer": outer, "inner": inner})
            idx += 1

        cap.release()
        return timeline

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

        total_time = timeline[-1]["t"]

        results = {}

        # ── #9  LEDs start OFF ──────────────────────────────────────
        early = [s for s in timeline if s["t"] < 2.0]
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
        for s in timeline:
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

        # For remaining checks, skip first 3 s of startup.
        running = [s for s in timeline if s["t"] > 3.0]
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
        late = [s for s in timeline if s["t"] > total_time - 30]
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
        for i in range(1, len(timeline)):
            if timeline[i]["inner"] != timeline[i - 1]["inner"]:
                change_times.append(timeline[i]["t"])

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
        for i in range(1, len(timeline)):
            prev, cur = timeline[i - 1], timeline[i]
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
    timeline = analyzer.extract_timeline(video_path)

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
