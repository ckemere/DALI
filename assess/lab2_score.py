"""
Lab 2 rubric scoring from video timeline data.

Phase 1 and Phase 2 reuse the Lab 1 clock-behavior checks (LED timing,
clockwise sequence, wrapping, etc.).

Phase 3 adds PWM-specific analysis:
  - Detect PWM modulation from brightness fluctuations
  - Estimate duty cycle (target: ~25%)
  - Estimate PWM frequency
  - Check for visible flicker (frequency too low)
  - Verify reduced brightness vs. full-on

For PWM detection, videos should be recorded at high frame rates
(120 fps or higher) so that individual PWM on/off cycles are visible
in the frame data.
"""

try:
    import numpy as np
except ImportError:
    np = None

from assess.lab1_score import (
    score as lab1_score,
    VIDEO_RUBRIC_ITEMS as LAB1_VIDEO_RUBRIC_ITEMS,
    VIDEO_RUBRIC_POINTS as LAB1_VIDEO_RUBRIC_POINTS,
    VIDEO_RUBRIC_DESCRIPTIONS as LAB1_VIDEO_RUBRIC_DESCRIPTIONS,
    SCORE_FIELDS as LAB1_SCORE_FIELDS,
)


# ── Phase 1 / Phase 2 video rubric ──────────────────────────────
# Exactly the same items as Lab 1.  "Works" means the LED clock
# behaves identically (correct sequencing and timing).

PHASE1_VIDEO_RUBRIC_ITEMS = list(LAB1_VIDEO_RUBRIC_ITEMS)
PHASE1_VIDEO_RUBRIC_POINTS = dict(LAB1_VIDEO_RUBRIC_POINTS)
PHASE1_VIDEO_RUBRIC_DESCRIPTIONS = dict(LAB1_VIDEO_RUBRIC_DESCRIPTIONS)
PHASE1_SCORE_FIELDS = list(LAB1_SCORE_FIELDS)


# ── Phase 3 video rubric (PWM-specific) ─────────────────────────

PHASE3_SCORE_FIELDS = list(LAB1_SCORE_FIELDS) + [
    "pwm_detected",
    "pwm_frequency_hz",
    "pwm_duty_cycle_pct",
    "brightness_reduction_pct",
    "no_visible_flicker",
]

PHASE3_VIDEO_RUBRIC_ITEMS = list(LAB1_VIDEO_RUBRIC_ITEMS) + [
    "reduced_brightness",
    "no_visible_flicker",
]

PHASE3_VIDEO_RUBRIC_POINTS = dict(LAB1_VIDEO_RUBRIC_POINTS)
PHASE3_VIDEO_RUBRIC_POINTS.update({
    "reduced_brightness":  1,
    "no_visible_flicker":  1,
})

PHASE3_VIDEO_RUBRIC_DESCRIPTIONS = dict(LAB1_VIDEO_RUBRIC_DESCRIPTIONS)
PHASE3_VIDEO_RUBRIC_DESCRIPTIONS.update({
    "reduced_brightness":  "LED brightness clearly reduced (duty cycle < 100%)",
    "no_visible_flicker":  "No visible flicker (PWM frequency sufficiently high)",
})

# `pwm_detected` is still computed and written to PHASE3_SCORE_FIELDS as
# a diagnostic, but it's deliberately excluded from the rubric: at the
# 30 fps sampling we typically use, the low-fps path in _analyze_pwm
# can't resolve individual PWM cycles and just infers pwm_detected
# from reduced_brightness, so it would be a double-count of the same
# signal.  Graders should look at reduced_brightness for the
# dimming-works check.

PHASE3_VIDEO_RUBRIC_MAX_POINTS = sum(
    PHASE3_VIDEO_RUBRIC_POINTS.get(k, 1) for k in PHASE3_VIDEO_RUBRIC_ITEMS
)


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


def score_phase1(timeline):
    """Score Phase 1 video — identical to Lab 1."""
    return lab1_score(timeline)


def score_phase2(timeline):
    """Score Phase 2 video — identical to Lab 1 (same clock behavior)."""
    return lab1_score(timeline)


def _analyze_pwm(raw_timeline, analyzer, baseline=None):
    """
    Analyze raw brightness data for PWM characteristics.

    Instead of the boolean on/off timeline used by Lab 1 scoring,
    this looks at the continuous brightness values to detect PWM
    modulation.

    Args:
        raw_timeline: List of dicts from a modified extract that
                      includes raw brightness values (not just bool).
        analyzer:     The VideoAnalyzer instance (for LED positions and
                      thresholds).
        baseline:     Optional per-ring ON/OFF brightness summary from
                      an earlier phase (output of
                      lab1_score._brightness_baseline).  When present,
                      the `reduced_brightness` check compares Phase 3
                      ON-LED brightness to this reference instead of
                      guessing "full" from the calibration threshold,
                      which is much more reliable across boards and
                      lighting conditions.

    Returns:
        dict with keys: pwm_detected, pwm_frequency_hz,
        pwm_duty_cycle_pct, brightness_reduction_pct, no_visible_flicker.
    """
    results = {}

    if not raw_timeline or len(raw_timeline) < 10:
        return {
            "pwm_detected": "NO_DATA",
            "pwm_frequency_hz": "",
            "pwm_duty_cycle_pct": "",
            "brightness_reduction_pct": "",
            "no_visible_flicker": "NO_DATA",
        }

    # Filter to post-programming frames (t >= 0).
    frames = [f for f in raw_timeline if f["t"] >= 0]
    if len(frames) < 10:
        return {
            "pwm_detected": "NO_DATA",
            "pwm_frequency_hz": "",
            "pwm_duty_cycle_pct": "",
            "brightness_reduction_pct": "",
            "no_visible_flicker": "NO_DATA",
        }

    # Infer the frame rate from timestamps.
    dts = np.diff([f["t"] for f in frames])
    fps = 1.0 / np.median(dts) if np.median(dts) > 0 else 30.0

    # Collect per-frame brightness for LEDs that are "active" (i.e.,
    # should be on based on the clock pattern).  We look at all LEDs
    # and find those that are on for a significant fraction of the video.
    outer_bri = np.array([f["outer_brightness"] for f in frames])  # (N, 12)
    inner_bri = np.array([f["inner_brightness"] for f in frames])  # (N, 12)

    # For each LED, compute the fraction of frames where it's above
    # a low threshold (indicating it's "intended to be on").
    outer_thr = analyzer.outer_threshold
    inner_thr = analyzer.inner_threshold
    # Use half the threshold as a "definitely off" baseline.
    low_outer = outer_thr * 0.3
    low_inner = inner_thr * 0.3

    # Find LEDs that are active at some point.
    outer_ever_on = np.any(outer_bri > low_outer, axis=0)
    inner_ever_on = np.any(inner_bri > low_inner, axis=0)

    # For active LEDs, collect brightness timeseries during periods
    # when they should be on.
    all_brightness_series = []
    for led_idx in range(12):
        if outer_ever_on[led_idx]:
            series = outer_bri[:, led_idx]
            # Only include segments where the LED is active.
            active_mask = series > outer_thr
            if np.sum(active_mask) > 10:
                all_brightness_series.append(series[active_mask])
        if inner_ever_on[led_idx]:
            series = inner_bri[:, led_idx]
            active_mask = series > inner_thr
            if np.sum(active_mask) > 10:
                all_brightness_series.append(series[active_mask])

    if not all_brightness_series:
        return {
            "pwm_detected": "FAIL (no active LEDs found)",
            "pwm_frequency_hz": "",
            "pwm_duty_cycle_pct": "",
            "brightness_reduction_pct": "",
            "no_visible_flicker": "NO_DATA",
        }

    # ── Detect PWM from brightness variation ──
    # For each active LED period, compute the coefficient of variation
    # (std/mean).  High CV indicates PWM; low CV indicates always-on.
    cvs = []
    duty_cycles = []
    for series in all_brightness_series:
        mean_b = np.mean(series)
        std_b = np.std(series)
        if mean_b > 0:
            cvs.append(std_b / mean_b)
        # Estimate duty cycle: fraction of frames above the full threshold.
        full_thr = max(outer_thr, inner_thr)
        duty = np.mean(series > full_thr * 0.8)
        duty_cycles.append(duty)

    avg_cv = np.mean(cvs)
    avg_duty = np.mean(duty_cycles) * 100  # as percentage

    # ── Brightness reduction ──
    # Preferred path: compare Phase 3 ON-LED brightness against the
    # per-ring "ON" distribution measured during Phase 1/2 (when the
    # LEDs were at full brightness).  This is calibration-invariant
    # and robust to the Phase 3 threshold being tuned for PWM'd LEDs.
    #
    # Fallback path (no baseline): guess "full" as ~1.5x the threshold.
    # This is sensitive to calibration choice and systematically
    # under-reports reduction when the Phase 3 threshold has been
    # lowered to catch PWM'd LEDs, but it's the best we can do without
    # a reference.
    reduction_source = "threshold"
    baseline_ratio = None
    if (baseline is not None
            and baseline.get("outer_on") is not None
            and baseline.get("inner_on") is not None):
        ring_ratios = []
        detail_bits = []
        for ring_name, bri_arr, low in (
            ("outer", outer_bri, low_outer),
            ("inner", inner_bri, low_inner),
        ):
            ring_on_samples = bri_arr[bri_arr > low]
            base = baseline.get(ring_name + "_on") or {}
            base_mean = base.get("mean", 0.0)
            if ring_on_samples.size > 0 and base_mean > 0:
                ring_mean = float(np.mean(ring_on_samples))
                ring_ratios.append(ring_mean / base_mean)
                detail_bits.append(
                    f"{ring_name} {ring_mean:.0f}/{base_mean:.0f}"
                )
        if ring_ratios:
            baseline_ratio = float(np.mean(ring_ratios))
            brightness_ratio = baseline_ratio
            reduction = max(0.0, (1.0 - brightness_ratio) * 100.0)
            reduction_source = "baseline"
            reduction_detail = ", ".join(detail_bits)

    if reduction_source == "threshold":
        all_active_brightness = np.concatenate(all_brightness_series)
        avg_brightness = np.mean(all_active_brightness)
        ref_brightness = max(outer_thr, inner_thr)
        full_brightness_estimate = ref_brightness * 1.5
        brightness_ratio = (avg_brightness / full_brightness_estimate
                            if full_brightness_estimate > 0 else 1.0)
        reduction = max(0.0, (1.0 - brightness_ratio) * 100.0)
        reduction_detail = f"vs {ref_brightness:.0f} threshold"

    results["brightness_reduction_pct"] = (
        f"{reduction:.0f}% ({reduction_source})"
    )

    # ── Branch: low-fps vs high-fps analysis ──
    # Above ~60 fps the camera can resolve individual PWM cycles, so we
    # use the CV / FFT path.  At lower frame rates (typical built-in
    # webcams cap at 30 fps) the camera integrates over many PWM
    # periods per frame, so CV becomes a *flicker* signal and we infer
    # PWM presence from brightness reduction instead.
    LOW_FPS_THRESHOLD = 60.0
    low_fps_mode = fps < LOW_FPS_THRESHOLD

    if low_fps_mode:
        REDUCTION_PASS = 25.0  # >=25% dimmer than full → clearly PWMing
        REDUCTION_FAIL = 10.0  # <10% reduction → essentially full-on
        CV_STEADY = 0.10       # smooth at 30 fps → PWM > camera Nyquist
        CV_FLICKER = 0.25      # ripply at 30 fps → PWM in/below visible band

        ref_label = ("Phase 1/2 baseline" if reduction_source == "baseline"
                     else "full-on estimate")
        if reduction >= REDUCTION_PASS:
            results["reduced_brightness"] = (
                f"PASS ({brightness_ratio*100:.0f}% of {ref_label}, "
                f"{reduction:.0f}% reduction; {reduction_detail})"
            )
            results["pwm_detected"] = (
                f"PASS (inferred from brightness reduction at "
                f"{fps:.0f} fps; individual PWM cycles not resolvable)"
            )
        elif reduction >= REDUCTION_FAIL:
            results["reduced_brightness"] = (
                f"PARTIAL ({brightness_ratio*100:.0f}% of {ref_label}, "
                f"only {reduction:.0f}% reduction; {reduction_detail})"
            )
            results["pwm_detected"] = (
                f"PARTIAL (mild dimming observed at {fps:.0f} fps)"
            )
        else:
            results["reduced_brightness"] = (
                f"FAIL ({brightness_ratio*100:.0f}% of {ref_label}; "
                f"no clear dimming; {reduction_detail})"
            )
            results["pwm_detected"] = (
                f"FAIL (no observable dimming at {fps:.0f} fps)"
            )

        # Duty cycle estimate is unreliable at low fps; report it as
        # an estimate so graders don't over-trust it.
        results["pwm_duty_cycle_pct"] = (
            f"~{avg_duty:.0f}% (low-fps estimate)"
        )

        # Flicker assessment via CV at the camera frame rate.  At
        # 30 fps, low CV means PWM frequency is well above the camera's
        # Nyquist (15 Hz) — and incidentally above the human flicker
        # fusion threshold of ~50 Hz.  High CV means the PWM is slow
        # enough to alias into the camera's band, which means it's
        # also visible to humans.
        if reduction >= REDUCTION_FAIL:
            if avg_cv < CV_STEADY:
                results["pwm_frequency_hz"] = (
                    f">{fps/2:.0f} (above {fps:.0f} fps Nyquist)"
                )
                results["no_visible_flicker"] = (
                    f"PASS (steady at {fps:.0f} fps, CV={avg_cv:.2f}; "
                    f"PWM frequency above human flicker threshold)"
                )
            elif avg_cv < CV_FLICKER:
                results["pwm_frequency_hz"] = (
                    f"~{fps/2:.0f} Hz (near {fps:.0f} fps Nyquist)"
                )
                results["no_visible_flicker"] = (
                    f"PARTIAL (some fluctuation at {fps:.0f} fps, "
                    f"CV={avg_cv:.2f})"
                )
            else:
                results["pwm_frequency_hz"] = (
                    f"<{fps/2:.0f} Hz (aliased at {fps:.0f} fps)"
                )
                results["no_visible_flicker"] = (
                    f"FAIL (high fluctuation at {fps:.0f} fps, "
                    f"CV={avg_cv:.2f}; PWM likely below visible "
                    f"flicker threshold)"
                )
        else:
            # No dimming observed → no PWM running → trivially no
            # flicker, but also no power savings (which the LLM
            # rubric will catch).
            results["pwm_frequency_hz"] = "N/A (no PWM detected)"
            results["no_visible_flicker"] = (
                "PASS (no PWM, no flicker — but also no power savings)"
            )

        return results

    # ── High-fps mode: original CV / FFT logic ──
    # PWM is detected if there's significant brightness variation
    # frame-to-frame.  With a 25% duty cycle, expect CV > 0.3.
    pwm_threshold_cv = 0.15
    pwm_detected = avg_cv > pwm_threshold_cv

    if pwm_detected:
        results["pwm_detected"] = f"PASS (CV={avg_cv:.2f})"
    else:
        results["pwm_detected"] = (
            f"FAIL (CV={avg_cv:.2f}, threshold={pwm_threshold_cv})"
        )

    # Duty cycle (high-fps).
    results["pwm_duty_cycle_pct"] = f"{avg_duty:.0f}%"

    # Prefer baseline-based reduction over duty-cycle heuristic when
    # we have a baseline — duty cycle via threshold is noisy, whereas
    # mean brightness vs. the Phase 1/2 ON distribution is not.
    if reduction_source == "baseline":
        ref_label = "Phase 1/2 baseline"
        if reduction >= 25.0:
            results["reduced_brightness"] = (
                f"PASS ({brightness_ratio*100:.0f}% of {ref_label}, "
                f"{reduction:.0f}% reduction; {reduction_detail})"
            )
        elif reduction >= 10.0:
            results["reduced_brightness"] = (
                f"PARTIAL ({brightness_ratio*100:.0f}% of {ref_label}, "
                f"only {reduction:.0f}% reduction; {reduction_detail})"
            )
        else:
            results["reduced_brightness"] = (
                f"FAIL ({brightness_ratio*100:.0f}% of {ref_label}; "
                f"no clear dimming; {reduction_detail})"
            )
    elif avg_duty < 80:
        results["reduced_brightness"] = (
            f"PASS (avg duty={avg_duty:.0f}%, "
            f"brightness_reduction={reduction:.0f}%)"
        )
    else:
        results["reduced_brightness"] = (
            f"FAIL (avg duty={avg_duty:.0f}%, no clear reduction)"
        )

    # ── PWM frequency estimation ──
    # Use FFT on the longest brightness series to find the dominant
    # frequency above 1 Hz.
    pwm_freq = None
    if pwm_detected and all_brightness_series:
        # Pick the longest series for best frequency resolution.
        longest = max(all_brightness_series, key=len)
        if len(longest) >= 20:
            # Detrend.
            detrended = longest - np.mean(longest)
            # Window to reduce spectral leakage.
            windowed = detrended * np.hanning(len(detrended))
            fft_vals = np.abs(np.fft.rfft(windowed))
            freqs = np.fft.rfftfreq(len(windowed), d=1.0 / fps)
            # Ignore DC and very low frequencies (< 2 Hz, which is
            # the clock tick rate).
            valid = freqs > 2.0
            if np.any(valid):
                fft_valid = fft_vals[valid]
                freq_valid = freqs[valid]
                peak_idx = np.argmax(fft_valid)
                pwm_freq = freq_valid[peak_idx]

    if pwm_freq is not None:
        results["pwm_frequency_hz"] = f"{pwm_freq:.1f}"
    else:
        results["pwm_frequency_hz"] = "undetected"

    # ── Flicker assessment ──
    # Visible flicker threshold: PWM frequency below ~50 Hz is
    # noticeable.  Above ~60 Hz is generally imperceptible.
    FLICKER_FAIL_HZ = 40
    FLICKER_PASS_HZ = 50

    if pwm_freq is not None:
        if pwm_freq >= FLICKER_PASS_HZ:
            results["no_visible_flicker"] = (
                f"PASS (PWM freq={pwm_freq:.1f} Hz, above {FLICKER_PASS_HZ} Hz)"
            )
        elif pwm_freq >= FLICKER_FAIL_HZ:
            results["no_visible_flicker"] = (
                f"PARTIAL (PWM freq={pwm_freq:.1f} Hz, borderline)"
            )
        else:
            results["no_visible_flicker"] = (
                f"FAIL (PWM freq={pwm_freq:.1f} Hz, below {FLICKER_FAIL_HZ} Hz)"
            )
    elif not pwm_detected:
        # No PWM detected — LEDs are always on, so no flicker but
        # also no power savings.
        results["no_visible_flicker"] = "PASS (no PWM, no flicker)"
    else:
        # PWM detected but frequency couldn't be measured (likely
        # because it's very high relative to our frame rate — good).
        results["no_visible_flicker"] = (
            "PASS (PWM detected but frequency above camera Nyquist limit)"
        )

    return results


def score_phase3(timeline, analyzer, baseline=None):
    """
    Score Phase 3 video: Lab 1 clock behavior + PWM analysis.

    Args:
        timeline: A list of frame dicts from VideoAnalyzer.extract_timeline().
                  Must include raw brightness fields (outer_brightness,
                  inner_brightness) for PWM analysis.
        analyzer: The VideoAnalyzer instance.
        baseline: Optional per-ring brightness baseline dict from an
                  earlier phase's score results (the
                  ``_brightness_baseline`` field set by lab1_score).
                  Enables a calibration-invariant reduced_brightness
                  check that compares Phase 3 ON-LED brightness to
                  Phase 1/2 full-on brightness.

    Returns:
        (results, changes, initial_outer, initial_inner)
        Same as lab1_score() but with additional PWM fields.
    """
    # Lab 1 scoring only touches the boolean fields; pass the full
    # timeline through.
    results, changes, initial_outer, initial_inner = lab1_score(timeline)

    # Then add PWM-specific analysis, which uses the raw brightness.
    pwm_results = _analyze_pwm(timeline, analyzer, baseline=baseline)
    results.update(pwm_results)

    return results, changes, initial_outer, initial_inner
