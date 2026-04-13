"""Lab 3 rubric item definitions and per-segment scoring helpers.

Lab 3 grading is segment-based: the capture pipeline records one
continuous video per student with N reflash/stimulus/observe blocks.
The analyzer slices the video at debug-LED transitions, extracts
per-segment LED state, and this module scores each segment against
its declared ``graded_items`` (defined in ``grading.lab3.segments``).

This module defines the canonical rubric item list, descriptions,
and default point values, but does NOT implement the video-analysis
logic itself — that lives in the analyzer (next step). The split
lets ``score_results.py`` export an editable rubric YAML without
needing OpenCV or a video file.
"""

from __future__ import annotations

from typing import Dict


# =====================================================================
# Video rubric items
# =====================================================================
# These are scored from per-segment video analysis.  Keys here must
# match the ``graded_items`` declared in ``grading.lab3.segments``.

VIDEO_RUBRIC_ITEMS = [
    # -- Segment 1: baseline clock operation -------------------------
    "normal_clock_runs",
    "normal_clock_timing_1hz",

    # -- Segment 2: debounce -----------------------------------------
    "debounce_rejects_glitch",

    # -- Segment 3: enter hour-set -----------------------------------
    "long_enters_hour_set",
    "hour_flashes_in_hour_set",
    "minute_steady_in_hour_set",
    "clock_does_not_advance_in_hour_set",

    # -- Segment 4: hour increment -----------------------------------
    "short_increments_hour",

    # -- Segment 5: hour wrap ----------------------------------------
    "hour_wraps_12_to_1",

    # -- Segment 6: enter minute-set ---------------------------------
    "long_enters_minute_set",
    "minute_flashes_in_minute_set",
    "hour_steady_in_minute_set",
    "clock_does_not_advance_in_minute_set",

    # -- Segment 7: minute increment ---------------------------------
    "short_increments_minute",

    # -- Segment 8: minute wrap --------------------------------------
    "minute_wraps_55_to_0",

    # -- Segment 9: return to normal (non-EC path: 3 longs) ----------
    "long_returns_to_normal",
    "clock_advances_after_return",
]

VIDEO_RUBRIC_DESCRIPTIONS: Dict[str, str] = {
    "normal_clock_runs":
        "Clock runs normally in Normal Clock Mode (both rings active)",
    "normal_clock_timing_1hz":
        "Clock timing is approximately 1 Hz",

    "debounce_rejects_glitch":
        "2 ms glitch press is rejected (no mode change or position change)",

    "long_enters_hour_set":
        "A long press transitions from Normal to Hour-Set mode",
    "hour_flashes_in_hour_set":
        "Hour indicator LED flashes in Hour-Set mode",
    "minute_steady_in_hour_set":
        "Minute indicator stays steady (not flashing) in Hour-Set mode",
    "clock_does_not_advance_in_hour_set":
        "Clock time does not advance while in Hour-Set mode",

    "short_increments_hour":
        "Short presses in Hour-Set mode advance the hour position",
    "hour_wraps_12_to_1":
        "Hour position wraps from 12 back to 1",

    "long_enters_minute_set":
        "A second long press transitions from Hour-Set to Minute-Set mode",
    "minute_flashes_in_minute_set":
        "Minute indicator LED flashes in Minute-Set mode",
    "hour_steady_in_minute_set":
        "Hour indicator stays steady (not flashing) in Minute-Set mode",
    "clock_does_not_advance_in_minute_set":
        "Clock time does not advance while in Minute-Set mode",

    "short_increments_minute":
        "Short presses in Minute-Set mode advance the minute position",
    "minute_wraps_55_to_0":
        "Minute position wraps from 55 (LED 11) back to 0 (LED 0)",

    "long_returns_to_normal":
        "Long press(es) cycle back to Normal Clock Mode",
    "clock_advances_after_return":
        "After returning to Normal, clock resumes advancing",
}

VIDEO_RUBRIC_POINTS: Dict[str, int] = {
    "normal_clock_runs":                    2,
    "normal_clock_timing_1hz":              2,

    "debounce_rejects_glitch":              2,

    "long_enters_hour_set":                 3,
    "hour_flashes_in_hour_set":             3,
    "minute_steady_in_hour_set":            1,
    "clock_does_not_advance_in_hour_set":   1,

    "short_increments_hour":                3,
    "hour_wraps_12_to_1":                   2,

    "long_enters_minute_set":               3,
    "minute_flashes_in_minute_set":         3,
    "hour_steady_in_minute_set":            1,
    "clock_does_not_advance_in_minute_set": 1,

    "short_increments_minute":              3,
    "minute_wraps_55_to_0":                 2,

    "long_returns_to_normal":               3,
    "clock_advances_after_return":          2,
}


# =====================================================================
# Brightness extra-credit rubric items (segments 10-12)
# =====================================================================

EC_VIDEO_RUBRIC_ITEMS = [
    "long_enters_brightness_set",
    "both_flash_in_brightness_set",
    "brightness_responds_to_short",
    "long_returns_to_normal_ec",
    "clock_advances_after_return_ec",
]

EC_VIDEO_RUBRIC_DESCRIPTIONS: Dict[str, str] = {
    "long_enters_brightness_set":
        "Three long presses enter Brightness-Set mode (EC)",
    "both_flash_in_brightness_set":
        "Both hour and minute LEDs flash in Brightness-Set mode (EC)",
    "brightness_responds_to_short":
        "Short presses change LED brightness in Brightness-Set mode (EC)",
    "long_returns_to_normal_ec":
        "Four long presses cycle back to Normal Clock Mode (EC path)",
    "clock_advances_after_return_ec":
        "After returning from brightness mode, clock resumes advancing (EC)",
}

EC_VIDEO_RUBRIC_POINTS: Dict[str, int] = {
    "long_enters_brightness_set":       2,
    "both_flash_in_brightness_set":     2,
    "brightness_responds_to_short":     2,
    "long_returns_to_normal_ec":        2,
    "clock_advances_after_return_ec":   2,
}


# =====================================================================
# LLM code review rubric items
# =====================================================================

LLM_RUBRIC_ITEMS = [
    "compiles",
    "button_gpio_init",
    "fsm_structure",
    "debounce_logic",
    "long_short_detection",
    "mode_transitions",
    "hour_increment_logic",
    "minute_increment_logic",
    "wrap_logic",
    "flash_via_fsm",
    "time_freeze_in_set_mode",
    "time_resume_on_exit",
    "readme_fsm_description",
    "readme_press_detection",
]

LLM_RUBRIC_DESCRIPTIONS: Dict[str, str] = {
    "compiles":
        "Code compiles without errors",
    "button_gpio_init":
        "PB8 configured as GPIO input (with pull-up or external pull)",
    "fsm_structure":
        "State machine structure is identifiable in the code",
    "debounce_logic":
        "Button debouncing implemented (rejects presses shorter than ~5 ms)",
    "long_short_detection":
        "Long-press vs. short-press detection implemented (threshold ~1 s)",
    "mode_transitions":
        "Mode transitions correct: Normal -> Hour-Set -> Minute-Set -> Normal",
    "hour_increment_logic":
        "Short press in Hour-Set mode increments the hour (with 12->1 wrap)",
    "minute_increment_logic":
        "Short press in Minute-Set mode increments the minute by 5 min",
    "wrap_logic":
        "Both hour (12->1) and minute (55->0) wrap-around implemented",
    "flash_via_fsm":
        "LED flashing in set modes uses the FSM (no blocking delays)",
    "time_freeze_in_set_mode":
        "Clock time does not advance during Hour-Set or Minute-Set modes",
    "time_resume_on_exit":
        "Newly set time becomes active when exiting set mode",
    "readme_fsm_description":
        "README describes the state machine design",
    "readme_press_detection":
        "README explains how long vs. short presses are detected",
}

LLM_RUBRIC_POINTS: Dict[str, int] = {
    "compiles":                 2,
    "button_gpio_init":         1,
    "fsm_structure":            3,
    "debounce_logic":           2,
    "long_short_detection":     2,
    "mode_transitions":         2,
    "hour_increment_logic":     1,
    "minute_increment_logic":   1,
    "wrap_logic":               1,
    "flash_via_fsm":            2,
    "time_freeze_in_set_mode":  1,
    "time_resume_on_exit":      1,
    "readme_fsm_description":   2,
    "readme_press_detection":   2,
}

# EC LLM items
EC_LLM_RUBRIC_ITEMS = [
    "brightness_mode_implemented",
    "brightness_levels_ge_15",
    "brightness_wrap_to_min",
]

EC_LLM_RUBRIC_DESCRIPTIONS: Dict[str, str] = {
    "brightness_mode_implemented":
        "Brightness-Set mode implemented as a third set mode after Minute-Set",
    "brightness_levels_ge_15":
        "At least 15 brightness levels defined",
    "brightness_wrap_to_min":
        "Brightness wraps from maximum back to minimum on short press",
}

EC_LLM_RUBRIC_POINTS: Dict[str, int] = {
    "brightness_mode_implemented":  2,
    "brightness_levels_ge_15":      2,
    "brightness_wrap_to_min":       2,
}


# =====================================================================
# Alternative-segment logic
# =====================================================================
# Some rubric items can be satisfied by more than one segment. This
# mapping tells the scorer: "if item X fails, also check item Y
# before declaring failure."  The scorer grants the points if EITHER
# passes.
#
# Currently only used for the return-to-normal test, which has both
# a 3-long variant (non-EC) and a 4-long variant (EC).

ALTERNATIVE_ITEMS: Dict[str, str] = {
    "long_returns_to_normal":      "long_returns_to_normal_ec",
    "clock_advances_after_return": "clock_advances_after_return_ec",
}


# =====================================================================
# Convenience aggregates
# =====================================================================

ALL_VIDEO_ITEMS = VIDEO_RUBRIC_ITEMS + EC_VIDEO_RUBRIC_ITEMS
ALL_VIDEO_DESCRIPTIONS = {**VIDEO_RUBRIC_DESCRIPTIONS, **EC_VIDEO_RUBRIC_DESCRIPTIONS}
ALL_VIDEO_POINTS = {**VIDEO_RUBRIC_POINTS, **EC_VIDEO_RUBRIC_POINTS}

ALL_LLM_ITEMS = LLM_RUBRIC_ITEMS + EC_LLM_RUBRIC_ITEMS
ALL_LLM_DESCRIPTIONS = {**LLM_RUBRIC_DESCRIPTIONS, **EC_LLM_RUBRIC_DESCRIPTIONS}
ALL_LLM_POINTS = {**LLM_RUBRIC_POINTS, **EC_LLM_RUBRIC_POINTS}

VIDEO_MAX_POINTS = sum(VIDEO_RUBRIC_POINTS.values())
EC_VIDEO_MAX_POINTS = sum(EC_VIDEO_RUBRIC_POINTS.values())
LLM_MAX_POINTS = sum(LLM_RUBRIC_POINTS.values())
EC_LLM_MAX_POINTS = sum(EC_LLM_RUBRIC_POINTS.values())


def video_verdict(raw_value) -> str:
    """Coerce a raw video score value to PASS / FAIL / NO_DATA."""
    if raw_value is None or raw_value == "NO_DATA" or raw_value == "":
        return "NO_DATA"
    val = str(raw_value).upper()
    if val.startswith("PASS"):
        return "PASS"
    if val.startswith("FAIL"):
        return "FAIL"
    if val in ("TRUE", "YES", "1"):
        return "PASS"
    if val in ("FALSE", "NO", "0"):
        return "FAIL"
    return "NO_DATA"
