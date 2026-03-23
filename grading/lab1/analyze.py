#!/usr/bin/env python3
"""
Standalone video analysis CLI for Lab 1.

Usage:
    python -m grading.lab1.analyze <video.mp4> <calibration.json>
"""

import sys

from grading.video_analyzer import VideoAnalyzer
from grading.lab1.score import score, SCORE_FIELDS


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Analyze Lab 1 LED clock video")
    parser.add_argument("video", help="Path to .mp4 video file")
    parser.add_argument("calibration", help="Path to calibration JSON")
    parser.add_argument("--outer-threshold", type=int, default=None,
                        help="Override brightness threshold for outer ring LEDs")
    parser.add_argument("--inner-threshold", type=int, default=None,
                        help="Override brightness threshold for inner ring LEDs")
    parser.add_argument("--debug-threshold", type=int, default=None,
                        help="Override brightness threshold for debug/programming LED")
    args = parser.parse_args()

    video_path, cal_path = args.video, args.calibration

    analyzer = VideoAnalyzer(cal_path)
    if args.outer_threshold is not None:
        analyzer.outer_threshold = args.outer_threshold
    if args.inner_threshold is not None:
        analyzer.inner_threshold = args.inner_threshold
    if args.debug_threshold is not None:
        analyzer.debug_threshold = args.debug_threshold
    print(f"Analyzing: {video_path}")
    print(f"Thresholds: outer={analyzer.outer_threshold}  "
          f"inner={analyzer.inner_threshold}  debug={analyzer.debug_threshold}  "
          f"Sample radius: {analyzer.radius}")
    timeline = analyzer.extract_timeline(video_path, verbose=True)

    if not timeline:
        print("No frames extracted.")
        sys.exit(1)

    print(f"Extracted {len(timeline)} samples over {timeline[-1]['t']:.1f}s\n")

    results, changes, initial_outer, initial_inner = score(timeline)

    print("=== Rubric Scores ===")
    for key in SCORE_FIELDS:
        val = results.get(key, "")
        if val:
            print(f"  {key:25s} {val}")

    print(f"\n=== State Changes (first 30 of {len(changes)}) ===")
    # Replay state to show which LEDs are on after each change
    outer_state = list(initial_outer)
    inner_state = list(initial_inner)
    for c in changes[:30]:
        for i in c["outer_on"]:
            outer_state[i] = True
        for i in c["outer_off"]:
            outer_state[i] = False
        for i in c["inner_on"]:
            inner_state[i] = True
        for i in c["inner_off"]:
            inner_state[i] = False
        outer_on = [i for i in range(12) if outer_state[i]]
        inner_on = [i for i in range(12) if inner_state[i]]
        print(f"  t={c['t']:7.2f}s  outer:{outer_on}  inner:{inner_on}")
    if len(changes) > 30:
        print(f"  ... and {len(changes) - 30} more")


if __name__ == "__main__":
    main()
