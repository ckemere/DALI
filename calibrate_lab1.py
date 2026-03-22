#!/usr/bin/env python3
"""
Lab 1 Calibration Script for DALI

Flashes a reference program onto the board and opens a live camera view
so the grader can click to mark each LED position.  The resulting
calibration file is used by the grading script for automated video
analysis.

Usage:
    python calibrate_lab1.py --camera 0 --output lab1_calibration.json
    python calibrate_lab1.py --flash reference.out --output lab1_calibration.json
    python calibrate_lab1.py --submission student.zip --output lab1_calibration.json
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
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

from grade_lab1 import (
    extract_submission,
    ensure_infrastructure,
    compile_submission,
    flash_firmware,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CCXML = os.path.join(SCRIPT_DIR, "MSPM0G3507.ccxml")
DEFAULT_OUTPUT = "lab1_calibration.json"
DEFAULT_THRESHOLD = 128
DEFAULT_SAMPLE_RADIUS = 15

# Each group: (key, display_label, count)
LED_GROUPS = [
    ("outer_ring", "Outer Ring (Hours)", 12),
    ("inner_ring", "Inner Ring (Seconds)", 12),
]


def find_dslite():
    path = os.environ.get("DSLITE_PATH")
    if path and os.path.isfile(path):
        return path
    return shutil.which("DSLite")


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
        (0, 165, 255),  # orange  – outer ring
        (0, 255, 0),    # green   – inner ring
    ]

    def __init__(self, camera_device=0, sample_radius=DEFAULT_SAMPLE_RADIUS):
        self.cap = cv2.VideoCapture(camera_device)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera device {camera_device}")

        self.positions = {key: [] for key, _, _ in LED_GROUPS}
        self.group_idx = 0
        self.threshold = DEFAULT_THRESHOLD
        self.sample_radius = sample_radius
        self.show_threshold = False
        self.frozen_frame = None

    # ── helpers ──────────────────────────────────────────────────────

    def _group(self):
        return LED_GROUPS[self.group_idx]

    def _all_done(self):
        return all(
            len(self.positions[key]) == count
            for key, _, count in LED_GROUPS
        )

    # ── mouse callback ──────────────────────────────────────────────

    def _on_click(self, event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
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

    # ── overlay ─────────────────────────────────────────────────────

    def _draw(self, frame):
        display = frame.copy()

        if self.show_threshold:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            _, mask = cv2.threshold(gray, self.threshold, 255,
                                    cv2.THRESH_BINARY)
            display = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)

        # Draw marked positions
        for gi, (key, _, _) in enumerate(LED_GROUPS):
            color = self.COLORS[gi % len(self.COLORS)]
            for i, pos in enumerate(self.positions[key]):
                cv2.circle(display, (pos["x"], pos["y"]),
                           self.sample_radius, color, 2)
                cv2.putText(display, str(i + 1),
                            (pos["x"] - 8, pos["y"] + 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # Instructions
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
        cv2.putText(
            display,
            f"threshold={self.threshold}  [t]oggle  [+/-]adjust  "
            f"[f]reeze  [u]ndo  [s]ave  [q]uit",
            (10, display.shape[0] - 10),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1,
        )
        return display

    # ── main loop ───────────────────────────────────────────────────

    def run(self):
        """Run the calibration GUI.  Returns a calibration dict or None."""
        win = "Lab 1 – LED Calibration"
        cv2.namedWindow(win)
        cv2.setMouseCallback(win, self._on_click)

        _, label, _ = self._group()
        print(f"Mark: {label}")
        print("Controls: click=mark, u=undo, t=threshold, +/-=adjust, "
              "f=freeze, s=save, q=quit\n")

        while True:
            if self.frozen_frame is None:
                ret, frame = self.cap.read()
                if not ret:
                    print("Camera read failed")
                    break
            else:
                frame = self.frozen_frame

            cv2.imshow(win, self._draw(frame))
            key = cv2.waitKey(30) & 0xFF

            if key == ord("q"):
                break
            elif key == ord("u"):
                # Undo last click; back-track to previous group if needed
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
            elif key in (ord("+"), ord("=")):
                self.threshold = min(255, self.threshold + 5)
                print(f"  Threshold: {self.threshold}")
            elif key == ord("-"):
                self.threshold = max(0, self.threshold - 5)
                print(f"  Threshold: {self.threshold}")
            elif key == ord("f"):
                if self.frozen_frame is None:
                    self.frozen_frame = frame.copy()
                    print("  Frame frozen")
                else:
                    self.frozen_frame = None
                    print("  Frame unfrozen")
            elif key == ord("s"):
                if not self._all_done():
                    print("  Not all LEDs marked yet!")
                    continue
                self.cap.release()
                cv2.destroyAllWindows()
                return {
                    "outer_ring": self.positions["outer_ring"],
                    "inner_ring": self.positions["inner_ring"],
                    "threshold": self.threshold,
                    "sample_radius": self.sample_radius,
                }

        self.cap.release()
        cv2.destroyAllWindows()
        return None


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Calibrate LED positions for Lab 1 video grading",
    )
    parser.add_argument(
        "--camera", type=int, default=0,
        help="Camera device index (default: 0)",
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

    # Flash a pre-compiled binary
    if args.flash:
        dslite = find_dslite()
        if not dslite:
            print("Error: DSLite not found. Set DSLITE_PATH or add to PATH.")
            sys.exit(1)
        print(f"Flashing: {args.flash}")
        ok, err = flash_binary(args.flash, args.ccxml, dslite)
        if ok:
            print("Flash OK\n")
        else:
            print(f"Flash FAILED: {err}")
            sys.exit(1)

    # Compile and flash a student submission zip
    if args.submission:
        dslite = find_dslite()
        if not dslite:
            print("Error: DSLite not found. Set DSLITE_PATH or add to PATH.")
            sys.exit(1)
        if not os.path.isfile(args.submission):
            print(f"Error: {args.submission} not found")
            sys.exit(1)

        build_dir = tempfile.mkdtemp(prefix="calibrate_")
        try:
            print(f"Extracting: {args.submission}")
            extract_submission(args.submission, build_dir)

            ok, err = ensure_infrastructure(build_dir)
            if not ok:
                print(f"Error: {err}")
                sys.exit(1)

            print("Compiling...")
            ok, stdout, stderr = compile_submission(build_dir)
            if not ok:
                print(f"Compile FAILED:\n{stderr[:500]}")
                sys.exit(1)
            print("Compile OK")

            print("Flashing...")
            ok, stdout, stderr = flash_firmware(build_dir, dslite, args.ccxml)
            if not ok:
                print(f"Flash FAILED: {stderr[:200]}")
                sys.exit(1)
            print("Flash OK\n")
        except zipfile.BadZipFile:
            print(f"Error: {args.submission} is not a valid zip file")
            sys.exit(1)
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

    # Run calibration
    gui = CalibrationGUI(args.camera, args.sample_radius)
    cal = gui.run()

    if cal:
        with open(args.output, "w") as f:
            json.dump(cal, f, indent=2)
        print(f"\nCalibration saved to {args.output}")
    else:
        print("\nCalibration cancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
