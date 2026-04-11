"""
Video-based LED board assessment.

Reads a recorded video of the LED board, extracts per-frame LED on/off
states using a calibration file, and detects the programming-to-running
transition via the debug LED.

Lab-specific scoring lives in separate modules (e.g. assess.lab1_score).

For the interactive calibration GUI, see grading.calibrate.
"""

import json

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


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
        debug = cal.get("debug_led", [])
        self.debug_pos = (debug[0]["x"], debug[0]["y"]) if debug else None
        self.radius = cal.get("sample_radius", 15)
        legacy_thr = cal.get("threshold", 128)
        self.outer_threshold = cal.get("outer_threshold", legacy_thr)
        self.inner_threshold = cal.get("inner_threshold", legacy_thr)
        self.debug_threshold = cal.get("debug_threshold", legacy_thr)

    def _brightness(self, gray, x, y):
        """Mean brightness in a circular patch around (x, y)."""
        r = self.radius
        h, w = gray.shape
        y1, y2 = max(0, y - r), min(h, y + r)
        x1, x2 = max(0, x - r), min(w, x + r)
        roi = gray[y1:y2, x1:x2]
        if roi.size == 0:
            return 0.0
        ry, rx = np.ogrid[:roi.shape[0], :roi.shape[1]]
        cy, cx = y - y1, x - x1
        mask = (rx - cx) ** 2 + (ry - cy) ** 2 <= r * r
        pixels = roi[mask]
        if pixels.size == 0:
            return 0.0
        return float(np.mean(pixels))

    def _detect_t0(self, debug_samples):
        """
        Find the moment programming ends by locating the last time the
        debug LED is on.
        """
        if not debug_samples:
            return 0.0
        last_on = None
        for t, is_on in debug_samples:
            if is_on:
                last_on = t
        return last_on if last_on is not None else 0.0

    def extract_timeline(self, video_path, sample_fps=0, verbose=False):
        """
        Sample LED brightness and on/off states from a video file.

        Returns a list of frame dicts:
            [{"t": float,                       # seconds since t0
              "outer": [bool]*12,               # outer ring on/off
              "inner": [bool]*12,               # inner ring on/off
              "debug": bool,                    # programming LED
              "outer_brightness": [float]*12,   # raw ROI means
              "inner_brightness": [float]*12,
              "debug_brightness": float}, ...]

        The boolean fields are computed by thresholding the raw
        brightness with self.outer_threshold / self.inner_threshold /
        self.debug_threshold.  Raw brightness values are always
        included so downstream code (Phase 3 PWM analysis, baseline
        collection) can re-threshold or re-aggregate as needed.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        skip = 1 if sample_fps <= 0 else max(1, int(fps / sample_fps))

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
                outer = [b > self.outer_threshold for b in outer_bri]
                inner = [b > self.inner_threshold for b in inner_bri]
                debug = debug_bri > self.debug_threshold

                if verbose and diag_count < 5:
                    all_bri = outer_bri + inner_bri
                    print(f"  [diag] t={t:.2f}s  "
                          f"outer_thr={self.outer_threshold}  "
                          f"inner_thr={self.inner_threshold}  "
                          f"debug={debug_bri:.0f} (thr={self.debug_threshold})  "
                          f"LED min={min(all_bri):.0f}  max={max(all_bri):.0f}  "
                          f"mean={np.mean(all_bri):.0f}  "
                          f"on={sum(outer)+sum(inner)}/24")
                    diag_count += 1

                raw.append({
                    "t": t,
                    "outer": outer,
                    "inner": inner,
                    "debug": debug,
                    "outer_brightness": outer_bri,
                    "inner_brightness": inner_bri,
                    "debug_brightness": debug_bri,
                })
            idx += 1

        cap.release()

        if verbose and raw:
            mid = raw[len(raw) // 2]
            print(f"  [diag] mid-video t={mid['t']:.2f}s  "
                  f"on={sum(mid['outer'])+sum(mid['inner'])}/24")

        # Detect t0 from debug LED
        t0 = 0.0
        if self.debug_pos and raw:
            debug_samples = [(s["t"], s["debug"]) for s in raw]
            t0 = self._detect_t0(debug_samples)

        for s in raw:
            s["t"] = round(s["t"] - t0, 3)

        return raw
