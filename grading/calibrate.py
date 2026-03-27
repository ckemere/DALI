#!/usr/bin/env python3
"""
LED Calibration GUI for DALI grading.

Shared across all labs that use the LED board.  Opens a live camera
view (or a pre-recorded video) so the grader can click to mark each
LED position.  The resulting calibration JSON is consumed by the
video analyzer.

Usage:
    python -m grading.calibrate --camera 0 --output calibration.json
    python -m grading.calibrate --video recording.mp4 --output calibration.json
    python -m grading.calibrate --flash reference.out --output calibration.json
    python -m grading.calibrate --submission student.zip --lab lab1 --output calibration.json
"""

import argparse
import json
import os
import subprocess
import shutil
import sys
import tempfile
import zipfile

try:
    import cv2
    import numpy as np
except ImportError:
    print("Error: opencv-python and numpy are required.")
    print("  pip install opencv-python numpy")
    sys.exit(1)

from assess.build import (
    extract_submission,
    ensure_infrastructure,
    compile_submission,
    find_dslite,
    DEFAULT_CCXML,
)

DEFAULT_OUTPUT = "calibration.json"
DEFAULT_THRESHOLD = 128
DEFAULT_SAMPLE_RADIUS = 15

# Each group: (key, display_label, count)
LED_GROUPS = [
    ("debug_led", "Debug/Programming LED", 1),
    ("outer_ring", "Outer Ring (Hours)", 12),
    ("inner_ring", "Inner Ring (Seconds)", 12),
]


def flash_binary(binary_path, ccxml_path, dslite_path):
    """Flash a .out binary onto the board."""
    proc = subprocess.run(
        [dslite_path, "flash", "--config", os.path.abspath(ccxml_path),
         "-f", binary_path],
        capture_output=True, text=True, timeout=30,
    )
    return proc.returncode == 0, proc.stderr.strip()


class CalibrationGUI:
    """Interactive GUI for marking LED positions on a live camera feed."""

    COLORS = [
        (0, 0, 255),    # red     – debug LED
        (0, 165, 255),  # orange  – outer ring
        (0, 255, 0),    # green   – inner ring
    ]

    DRAG_THRESHOLD = 20  # pixels – click within this to grab an existing point

    def __init__(self, camera_device=0, sample_radius=DEFAULT_SAMPLE_RADIUS,
                 video_path=None):
        if video_path:
            self.cap = cv2.VideoCapture(video_path)
            if not self.cap.isOpened():
                raise RuntimeError(f"Cannot open video file {video_path}")
            self._video_path = video_path
        else:
            self.cap = cv2.VideoCapture(camera_device)
            if not self.cap.isOpened():
                raise RuntimeError(f"Cannot open camera device {camera_device}")
            self._video_path = None

        self.positions = {key: [] for key, _, _ in LED_GROUPS}
        self.group_idx = 0
        self.debug_threshold = DEFAULT_THRESHOLD
        self.outer_threshold = DEFAULT_THRESHOLD
        self.inner_threshold = DEFAULT_THRESHOLD
        # Which threshold +/- adjusts: 0=outer, 1=inner, 2=debug
        self._thr_select = 0
        self.sample_radius = sample_radius
        self.show_threshold = False
        self.show_brightness = True
        self.frozen_frame = None
        # Drag state: (group_key, index) of the point being dragged
        self._dragging = None
        # Brightness tracking: {(group_key, index): [min, max, count, sum]}
        self._brightness_stats = {}

    # ── helpers ──────────────────────────────────────────────────────

    def _brightness(self, gray, x, y):
        """Mean brightness in a circular patch around (x, y)."""
        r = self.sample_radius
        h, w = gray.shape
        y1, y2 = max(0, y - r), min(h, y + r)
        x1, x2 = max(0, x - r), min(w, x + r)
        roi = gray[y1:y2, x1:x2]
        if roi.size == 0:
            return 0.0
        # Build a circular mask within the ROI
        ry, rx = np.ogrid[:roi.shape[0], :roi.shape[1]]
        cy, cx = y - y1, x - x1
        mask = (rx - cx) ** 2 + (ry - cy) ** 2 <= r * r
        pixels = roi[mask]
        if pixels.size == 0:
            return 0.0
        return float(np.mean(pixels))

    def _update_brightness_stats(self, gray):
        """Sample brightness at every marked position and update running stats."""
        for key, _, _ in LED_GROUPS:
            for i, pos in enumerate(self.positions[key]):
                bri = self._brightness(gray, pos["x"], pos["y"])
                stat_key = (key, i)
                if stat_key not in self._brightness_stats:
                    self._brightness_stats[stat_key] = [bri, bri, 1, bri]
                else:
                    s = self._brightness_stats[stat_key]
                    s[0] = min(s[0], bri)
                    s[1] = max(s[1], bri)
                    s[2] += 1
                    s[3] += bri

    def _reset_brightness_stats(self):
        """Clear accumulated stats (e.g. after changing LED state)."""
        self._brightness_stats.clear()

    def _print_brightness_summary(self):
        """Print per-LED brightness stats and suggest a threshold."""
        if not self._brightness_stats:
            print("  No brightness data yet. Mark LEDs and wait a moment.")
            return
        print("\n  === Brightness Summary ===")
        print(f"  {'Group':<15} {'LED':>3}  {'Min':>5}  {'Max':>5}  {'Avg':>5}")
        print(f"  {'-'*45}")
        all_mins = []
        all_maxs = []
        for key, label, _ in LED_GROUPS:
            for i in range(len(self.positions[key])):
                stat_key = (key, i)
                if stat_key in self._brightness_stats:
                    s = self._brightness_stats[stat_key]
                    mn, mx, cnt, total = s
                    avg = total / cnt
                    short_label = label.split("(")[0].strip()
                    print(f"  {short_label:<15} {i+1:>3}  {mn:5.0f}  {mx:5.0f}  {avg:5.0f}")
                    all_mins.append(mn)
                    all_maxs.append(mx)
        if all_mins:
            global_min = min(all_mins)
            global_max = max(all_maxs)
            max_of_mins = max(all_mins)
            min_of_maxes = min(all_maxs)
            print(f"\n  Overall range: {global_min:.0f} - {global_max:.0f}")
            print(f"  Highest 'min': {max_of_mins:.0f}  (dimmest an LED ever was)")
            print(f"  Lowest 'max':  {min_of_maxes:.0f}  (brightest the dimmest LED got)")
            if max_of_mins < min_of_maxes:
                suggested = int((max_of_mins + min_of_maxes) / 2)
                print(f"  Suggested threshold: {suggested}  "
                      f"(midpoint of {max_of_mins:.0f}..{min_of_maxes:.0f} gap)")
            else:
                print(f"  No clear gap — try pressing 'r' to reset, then "
                      f"observe with some LEDs ON and some OFF")
            print(f"  Current thresholds: outer={self.outer_threshold}  "
                  f"inner={self.inner_threshold}  debug={self.debug_threshold}")
        print()

    def _group(self):
        return LED_GROUPS[self.group_idx]

    def _all_done(self):
        return all(
            len(self.positions[key]) == count
            for key, _, count in LED_GROUPS
        )

    def _find_nearest(self, x, y):
        """Find the nearest existing point across all groups.
        Returns (group_key, index, distance) or None."""
        best = None
        for key, _, _ in LED_GROUPS:
            for i, pos in enumerate(self.positions[key]):
                d = ((pos["x"] - x) ** 2 + (pos["y"] - y) ** 2) ** 0.5
                if best is None or d < best[2]:
                    best = (key, i, d)
        return best

    # ── mouse callback ──────────────────────────────────────────────

    def _on_mouse(self, event, x, y, flags, param):
        # ── Right-click: delete nearest point ──
        if event == cv2.EVENT_RBUTTONDOWN:
            nearest = self._find_nearest(x, y)
            if nearest and nearest[2] < self.DRAG_THRESHOLD:
                gkey, idx, _ = nearest
                removed = self.positions[gkey].pop(idx)
                glabel = next(
                    lbl for k, lbl, _ in LED_GROUPS if k == gkey
                )
                print(f"  Deleted {glabel} LED {idx + 1} "
                      f"at ({removed['x']}, {removed['y']})")
                for gi, (k, _, cnt) in enumerate(LED_GROUPS):
                    if len(self.positions[k]) < cnt:
                        self.group_idx = gi
                        break
            return

        # ── Left-button down: start drag or place new point ──
        if event == cv2.EVENT_LBUTTONDOWN:
            nearest = self._find_nearest(x, y)
            if nearest and nearest[2] < self.DRAG_THRESHOLD:
                self._dragging = (nearest[0], nearest[1])
                return

            key, label, count = self._group()
            if len(self.positions[key]) >= count:
                return
            self.positions[key].append({"x": x, "y": y})
            n = len(self.positions[key])
            print(f"  {label} LED {n}/{count} at ({x}, {y})")

            if n == count and self.group_idx < len(LED_GROUPS) - 1:
                self.group_idx += 1
                _, next_label, _ = self._group()
                print(f"\nNow mark: {next_label}")
            elif self._all_done():
                print("\nAll LEDs marked! Press 's' to save or 'q' to quit.")
            return

        # ── Mouse move while dragging ──
        if event == cv2.EVENT_MOUSEMOVE and self._dragging is not None:
            gkey, idx = self._dragging
            self.positions[gkey][idx] = {"x": x, "y": y}
            return

        # ── Left-button up: stop dragging ──
        if event == cv2.EVENT_LBUTTONUP and self._dragging is not None:
            gkey, idx = self._dragging
            pos = self.positions[gkey][idx]
            glabel = next(
                lbl for k, lbl, _ in LED_GROUPS if k == gkey
            )
            print(f"  Moved {glabel} LED {idx + 1} to ({pos['x']}, {pos['y']})")
            self._dragging = None
            return

    # ── overlay ─────────────────────────────────────────────────────

    def _draw(self, frame):
        display = frame.copy()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if self.show_threshold:
            _, mask = cv2.threshold(gray, self.outer_threshold, 255,
                                    cv2.THRESH_BINARY)
            display = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)

        self._update_brightness_stats(gray)

        on_count = 0
        off_count = 0
        all_brightness = []
        for gi, (key, _, _) in enumerate(LED_GROUPS):
            color = self.COLORS[gi % len(self.COLORS)]
            thr = (self.debug_threshold if key == "debug_led"
                   else self.inner_threshold if key == "inner_ring"
                   else self.outer_threshold)
            for i, pos in enumerate(self.positions[key]):
                bri = self._brightness(gray, pos["x"], pos["y"])
                is_on = bri > thr
                all_brightness.append(bri)
                if key != "debug_led":
                    if is_on:
                        on_count += 1
                    else:
                        off_count += 1

                is_dragged = (self._dragging is not None
                              and self._dragging == (key, i))
                thickness = 3 if is_dragged else 2
                if is_dragged:
                    draw_color = (255, 255, 255)
                elif is_on:
                    draw_color = (200, 50, 0)    # dark blue = ON
                else:
                    draw_color = color           # group color = OFF
                cv2.circle(display, (pos["x"], pos["y"]),
                           self.sample_radius, draw_color, thickness)

                label_text = str(i + 1)
                if self.show_brightness:
                    label_text = f"{int(bri)}"
                cv2.putText(display, label_text,
                            (pos["x"] - 8, pos["y"] + 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, draw_color, 1)

        key, label, count = self._group()
        placed = len(self.positions[key])
        if placed < count:
            text = f"Click {label} LED {placed + 1}/{count}"
        elif not self._all_done():
            text = "Group complete – moving to next..."
        else:
            text = "All done! Press 's' to save"

        cv2.putText(display, text, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        thr_names = ["outer", "inner", "debug"]
        adjusting = thr_names[self._thr_select]
        thr_text = (f"outer={self.outer_threshold}  inner={self.inner_threshold}  "
                    f"debug={self.debug_threshold}")
        if all_brightness:
            bmin, bmax = int(min(all_brightness)), int(max(all_brightness))
            stats_text = (f"thr: {thr_text}  ON={on_count} OFF={off_count}  "
                          f"range={bmin}-{bmax}  [+/-]->{adjusting}")
        else:
            stats_text = f"thr: {thr_text}  [+/-]->{adjusting}"
        cv2.putText(display, stats_text, (10, 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)

        cv2.putText(
            display,
            f"[t]hreshold [d]cycle-thr [+/-]adj [b]ri-stats [r]eset-stats  "
            f"drag=move  right-click=del  [u]ndo [f]reeze [s]ave [q]uit",
            (10, display.shape[0] - 10),
            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200, 200, 200), 1,
        )
        return display

    # ── main loop ───────────────────────────────────────────────────

    def run(self):
        """Run the calibration GUI.  Returns a calibration dict or None."""
        win = "LED Calibration"
        cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
        ret, frame = self.cap.read()
        if ret:
            cv2.imshow(win, frame)
            cv2.waitKey(1)
        cv2.setMouseCallback(win, self._on_mouse)

        _, label, _ = self._group()
        print(f"Mark: {label}")
        print("Mouse: left-click=place, drag=move, right-click=delete")
        print("Keys:  u=undo, t=threshold view, d=cycle threshold (outer/inner/debug),")
        print("       +/-=adjust selected threshold, b=brightness stats, r=reset stats,")
        print("       f=freeze, s=save, q=quit\n")

        while True:
            if self.frozen_frame is None:
                ret, frame = self.cap.read()
                if not ret:
                    if self._video_path:
                        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = self.cap.read()
                        if not ret:
                            print("Video read failed")
                            break
                    else:
                        print("Camera read failed")
                        break
            else:
                frame = self.frozen_frame

            cv2.imshow(win, self._draw(frame))
            key = cv2.waitKey(30) & 0xFF

            if key == ord("q"):
                break
            elif key == ord("u"):
                gkey, _, _ = self._group()
                if self.positions[gkey]:
                    p = self.positions[gkey].pop()
                    print(f"  Undid ({p['x']}, {p['y']})")
                elif self.group_idx > 0:
                    self.group_idx -= 1
                    gkey, _, _ = self._group()
                    if self.positions[gkey]:
                        p = self.positions[gkey].pop()
                        print(f"  Back to previous group; undid ({p['x']}, {p['y']})")
            elif key == ord("t"):
                self.show_threshold = not self.show_threshold
            elif key == ord("d"):
                self._thr_select = (self._thr_select + 1) % 3
                names = ["outer ring", "inner ring", "debug LED"]
                print(f"  +/- now adjusts: {names[self._thr_select]} threshold")
            elif key in (ord("+"), ord("=")):
                if self._thr_select == 0:
                    self.outer_threshold = min(255, self.outer_threshold + 5)
                    print(f"  Outer threshold: {self.outer_threshold}")
                elif self._thr_select == 1:
                    self.inner_threshold = min(255, self.inner_threshold + 5)
                    print(f"  Inner threshold: {self.inner_threshold}")
                else:
                    self.debug_threshold = min(255, self.debug_threshold + 5)
                    print(f"  Debug threshold: {self.debug_threshold}")
            elif key == ord("-"):
                if self._thr_select == 0:
                    self.outer_threshold = max(0, self.outer_threshold - 5)
                    print(f"  Outer threshold: {self.outer_threshold}")
                elif self._thr_select == 1:
                    self.inner_threshold = max(0, self.inner_threshold - 5)
                    print(f"  Inner threshold: {self.inner_threshold}")
                else:
                    self.debug_threshold = max(0, self.debug_threshold - 5)
                    print(f"  Debug threshold: {self.debug_threshold}")
            elif key == ord("f"):
                if self.frozen_frame is None:
                    self.frozen_frame = frame.copy()
                    print("  Frame frozen")
                else:
                    self.frozen_frame = None
                    print("  Frame unfrozen")
            elif key == ord("b"):
                self._print_brightness_summary()
            elif key == ord("r"):
                self._reset_brightness_stats()
                print("  Brightness stats reset")
            elif key == ord("s"):
                if not self._all_done():
                    print("  Not all LEDs marked yet!")
                    continue
                self.cap.release()
                cv2.destroyAllWindows()
                return {
                    "debug_led": self.positions["debug_led"],
                    "outer_ring": self.positions["outer_ring"],
                    "inner_ring": self.positions["inner_ring"],
                    "outer_threshold": self.outer_threshold,
                    "inner_threshold": self.inner_threshold,
                    "debug_threshold": self.debug_threshold,
                    "sample_radius": self.sample_radius,
                }

        self.cap.release()
        cv2.destroyAllWindows()
        return None


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Calibrate LED positions for video grading",
    )
    parser.add_argument(
        "--camera", type=int, default=0,
        help="Camera device index (default: 0)",
    )
    parser.add_argument(
        "--video", metavar="FILE",
        help="Use a pre-recorded video file instead of a live camera (loops automatically)",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"Output calibration JSON (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--flash", metavar="BINARY",
        help="Flash a pre-compiled .out binary onto the board before calibrating",
    )
    parser.add_argument(
        "--submission", metavar="ZIP",
        help="Compile and flash a student submission zip before calibrating",
    )
    parser.add_argument(
        "--lab", default="lab1",
        help="Lab name for template files when using --submission (default: lab1)",
    )
    parser.add_argument(
        "--ccxml", default=DEFAULT_CCXML,
        help="CCXML target config for DSLite (used with --flash/--submission)",
    )
    parser.add_argument(
        "--sample-radius", type=int, default=DEFAULT_SAMPLE_RADIUS,
        help=f"Pixel radius to sample around each LED (default: {DEFAULT_SAMPLE_RADIUS})",
    )
    args = parser.parse_args()

    if args.flash and args.submission:
        print("Error: use --flash or --submission, not both.")
        sys.exit(1)

    # Map lab name to output binary name
    lab_output_names = {"lab1": "Lab_1", "lab2": "Lab_2", "lab3": "Lab_3"}
    output_name = lab_output_names.get(args.lab, args.lab)

    flash_binary_path = None
    flash_build_dir = None
    dslite = None

    if args.flash or args.submission:
        dslite = find_dslite()
        if not dslite:
            print("Error: DSLite not found. Set DSLITE_PATH or add to PATH.")
            sys.exit(1)

    if args.flash:
        if not os.path.isfile(args.flash):
            print(f"Error: {args.flash} not found")
            sys.exit(1)
        flash_binary_path = args.flash

    if args.submission:
        if not os.path.isfile(args.submission):
            print(f"Error: {args.submission} not found")
            sys.exit(1)

        flash_build_dir = tempfile.mkdtemp(prefix="calibrate_")
        try:
            print(f"Extracting: {args.submission}")
            extract_submission(args.submission, flash_build_dir)

            ok, err = ensure_infrastructure(flash_build_dir, args.lab)
            if not ok:
                print(f"Error: {err}")
                sys.exit(1)

            print("Compiling...")
            ok, stdout, stderr = compile_submission(flash_build_dir, output_name)
            if not ok:
                print(f"Compile FAILED:\n{stderr[:500]}")
                sys.exit(1)
            print("Compile OK")

            flash_binary_path = os.path.join(flash_build_dir, f"{output_name}.out")
        except zipfile.BadZipFile:
            print(f"Error: {args.submission} is not a valid zip file")
            sys.exit(1)

    # Open camera FIRST so it's capturing before we flash.
    if args.video:
        if not os.path.isfile(args.video):
            print(f"Error: {args.video} not found")
            sys.exit(1)
        gui = CalibrationGUI(sample_radius=args.sample_radius,
                             video_path=args.video)
    else:
        gui = CalibrationGUI(args.camera, args.sample_radius)

    if flash_binary_path:
        print(f"Flashing: {flash_binary_path}")
        ok, err = flash_binary(flash_binary_path, args.ccxml, dslite)
        if ok:
            print("Flash OK\n")
        else:
            print(f"Flash FAILED: {err}")
            gui.cap.release()
            sys.exit(1)
        if flash_build_dir:
            shutil.rmtree(flash_build_dir, ignore_errors=True)
            flash_build_dir = None

    try:
        cal = gui.run()
    finally:
        if flash_build_dir:
            shutil.rmtree(flash_build_dir, ignore_errors=True)

    if cal:
        with open(args.output, "w") as f:
            json.dump(cal, f, indent=2)
        print(f"\nCalibration saved to {args.output}")
    else:
        print("\nCalibration cancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
