"""
Score raw analysis results and generate grades CSV and student reports
for Lab 2.

Reads video_results.json and/or llm_results.json (produced by grade.py),
applies rubric point weights from a YAML file, and outputs:
  - A simple grades CSV (student, per-item points, subtotals, grand total)
  - Optional per-student text grade reports

The video results are organized per-phase:
  {"student": {"phase1": {...scores...}, "phase2": {...}, "phase3": {...}}}

The LLM results are flat per-student (items already have phase prefixes):
  {"student": {"phase1_compiles": {...}, "phase2_timer_interrupt": {...}, ...}}

Usage:
    # Export default rubric for editing:
    python -m grading.lab2.score_results --export-rubric rubric.yaml

    # Score results:
    python -m grading.lab2.score_results \
        --video-results video_results.json \
        --llm-results llm_results.json \
        --rubric rubric.yaml \
        --grades-csv grades.csv \
        --reports-dir reports/
"""

import argparse
import csv
import json
import os
import sys

import yaml

from assess.build import student_name_from_zip
from assess.lab2_score import (
    PHASE1_VIDEO_RUBRIC_ITEMS,
    PHASE1_VIDEO_RUBRIC_POINTS,
    PHASE1_VIDEO_RUBRIC_DESCRIPTIONS,
    PHASE3_VIDEO_RUBRIC_ITEMS,
    PHASE3_VIDEO_RUBRIC_POINTS,
    PHASE3_VIDEO_RUBRIC_DESCRIPTIONS,
    video_verdict,
)
from assess.lab2_code_review import (
    RUBRIC_ITEMS as LLM_RUBRIC_ITEMS,
    RUBRIC_POINTS as LLM_RUBRIC_POINTS,
    RUBRIC_DESCRIPTIONS as LLM_RUBRIC_DESCRIPTIONS,
)


def _normalize_video_results(raw):
    """
    Collapse Canvas-wrapped student keys in a video_results.json to
    canonical student names and merge their phase entries.

    Canvas "Download Submissions" produces a different submission and
    attachment ID per phase assignment, so the same student ends up
    under three different keys (e.g. ``alice_78839_7441683_Lab_2_-_Phase_1_x``
    and ``alice_78839_7441690_Lab_2_-_Phase_2_x``) with only one phase
    populated each.  This function joins them back together.

    When the same (canonical_student, phase) pair shows up more than
    once — e.g. a resubmission with a ``-1`` suffix — the entry that
    sorts later wins (Canvas's attachment IDs are monotonic, so
    lexicographic order ≈ chronological order).
    """
    merged = {}
    dropped = 0
    for raw_key in sorted(raw.keys()):
        canon = student_name_from_zip(raw_key)
        phases = raw.get(raw_key) or {}
        if not isinstance(phases, dict):
            continue
        dest = merged.setdefault(canon, {})
        for phase, scores in phases.items():
            if phase in dest:
                dropped += 1
            dest[phase] = scores
    if len(merged) < len(raw):
        print(f"  Normalized video_results: {len(raw)} raw keys → "
              f"{len(merged)} canonical students"
              + (f" ({dropped} duplicate phase entries, later wins)"
                 if dropped else ""))
    return merged


def _normalize_llm_results(raw):
    """
    Collapse Canvas-wrapped student keys in an llm_results.json.

    LLM review assembles per-student code from all phases found under
    a single key, so if the upstream capture/review stage was run with
    broken student matching each "student" key only contains code from
    one phase and most rubric items will be FAIL/UNCLEAR.  We merge
    per-item verdicts across keys that share a canonical name, picking
    whichever variant actually has a PASS/FAIL verdict.  This is a
    best-effort repair; re-running the LLM step after this fix lands
    will give better results.
    """
    merged = {}
    for raw_key in sorted(raw.keys()):
        canon = student_name_from_zip(raw_key)
        items = raw.get(raw_key) or {}
        if not isinstance(items, dict):
            continue
        dest = merged.setdefault(canon, {})
        for item_id, entry in items.items():
            existing = dest.get(item_id)
            if existing is None:
                dest[item_id] = entry
                continue
            # Prefer entries with a concrete verdict over UNCLEAR/missing.
            def _rank(e):
                if not isinstance(e, dict):
                    return 0
                v = str(e.get("verdict", "")).upper()
                if v == "PASS":
                    return 3
                if v == "FAIL":
                    return 2
                if v:
                    return 1
                return 0
            if _rank(entry) > _rank(existing):
                dest[item_id] = entry
    if len(merged) < len(raw):
        print(f"  Normalized llm_results: {len(raw)} raw keys → "
              f"{len(merged)} canonical students")
    return merged

# For Phases 1 and 2, the video rubric items are the same as Lab 1.
# Phase 3 adds PWM-specific items.
# We define a combined structure that maps (phase, item) -> points.

# Each phase's video rubric items that produce a PASS/FAIL verdict.
PHASE_VIDEO_ITEMS = {
    "phase1": list(PHASE1_VIDEO_RUBRIC_ITEMS),
    "phase2": list(PHASE1_VIDEO_RUBRIC_ITEMS),  # same checks
    "phase3": list(PHASE3_VIDEO_RUBRIC_ITEMS),
}

PHASE_VIDEO_POINTS = {
    "phase1": dict(PHASE1_VIDEO_RUBRIC_POINTS),
    "phase2": dict(PHASE1_VIDEO_RUBRIC_POINTS),
    "phase3": dict(PHASE3_VIDEO_RUBRIC_POINTS),
}

PHASE_VIDEO_DESCRIPTIONS = {
    "phase1": dict(PHASE1_VIDEO_RUBRIC_DESCRIPTIONS),
    "phase2": dict(PHASE1_VIDEO_RUBRIC_DESCRIPTIONS),
    "phase3": dict(PHASE3_VIDEO_RUBRIC_DESCRIPTIONS),
}


def load_rubric(rubric_path):
    """Load rubric weights from YAML, updating the module-level dicts.

    Returns (total_video_max, total_llm_max, total_power_sanity_max).
    """
    with open(rubric_path, "r") as f:
        data = yaml.safe_load(f)

    for phase_key in ("phase1_video", "phase2_video", "phase3_video"):
        phase = phase_key.replace("_video", "")
        for entry in data.get(phase_key, []):
            item_id = entry["id"]
            if item_id in PHASE_VIDEO_POINTS.get(phase, {}):
                PHASE_VIDEO_POINTS[phase][item_id] = entry["points"]
                if "description" in entry:
                    PHASE_VIDEO_DESCRIPTIONS[phase][item_id] = entry["description"]

    for entry in data.get("llm_rubric", []):
        item_id = entry["id"]
        if item_id in LLM_RUBRIC_POINTS:
            LLM_RUBRIC_POINTS[item_id] = entry["points"]
            if "description" in entry:
                LLM_RUBRIC_DESCRIPTIONS[item_id] = entry["description"]

    for entry in data.get("power_sanity", []):
        item_id = entry["id"]
        if item_id in POWER_SANITY_POINTS:
            POWER_SANITY_POINTS[item_id] = entry["points"]
            if "description" in entry:
                POWER_SANITY_DESCRIPTIONS[item_id] = entry["description"]
            if "threshold_uA" in entry:
                POWER_SANITY_THRESHOLDS[item_id] = float(entry["threshold_uA"])

    vid_max = sum(
        sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
        for p, items in PHASE_VIDEO_ITEMS.items()
    )
    llm_max = sum(LLM_RUBRIC_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)
    power_max = sum(POWER_SANITY_POINTS.get(k, 0) for k in POWER_SANITY_ITEMS)
    return vid_max, llm_max, power_max


def export_rubric(path):
    """Write default rubric YAML for user editing."""
    data = {}
    for phase in ("phase1", "phase2", "phase3"):
        key = f"{phase}_video"
        items = PHASE_VIDEO_ITEMS[phase]
        descs = PHASE_VIDEO_DESCRIPTIONS[phase]
        pts = PHASE_VIDEO_POINTS[phase]
        data[key] = [
            {"id": k, "description": descs.get(k, k), "points": pts.get(k, 1)}
            for k in items
        ]

    data["llm_rubric"] = [
        {"id": k,
         "description": LLM_RUBRIC_DESCRIPTIONS.get(k, k),
         "points": LLM_RUBRIC_POINTS.get(k, 1)}
        for k in LLM_RUBRIC_ITEMS
    ]

    data["power_sanity"] = [
        {"id": k,
         "description": POWER_SANITY_DESCRIPTIONS.get(k, k),
         "points": POWER_SANITY_POINTS.get(k, 0),
         "threshold_uA": POWER_SANITY_THRESHOLDS.get(
             k, DEFAULT_POWER_THRESHOLD_uA)}
        for k in POWER_SANITY_ITEMS
    ]

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# Mapping from phase name to the LLM rubric item whose response carries
# that phase's extracted power measurement.
_PHASE_POWER_ITEM = {
    "phase1": "phase1_baseline_documented",
    "phase2": "phase2_sleep_power_documented",
    "phase3": "phase3_pwm_power_documented",
}

# Default plausibility threshold.  An MSPM0G3507 LaunchPad driving the
# LED clock should draw well under 10 mA even in the Phase 1 busy-wait
# baseline; readings > 10 mA almost always mean the student left the
# analog-power (3V3 ANA / J101) jumper in place, so the EnergyTrace
# reading also includes the XDS debug probe and on-board analog rail.
DEFAULT_POWER_THRESHOLD_uA = 10000.0

# Computed "power_sanity" rubric category.  Structured the same way as
# the video / LLM rubric dicts so load_rubric / export_rubric /
# score_student / generate_grades_csv treat it uniformly.
POWER_SANITY_ITEMS = ["power_plausible"]

POWER_SANITY_POINTS = {
    # Default of 2; instructor overrides in rubric.yaml if they want
    # a different deduction (or 0 to treat it as diagnostic-only).
    "power_plausible": 2,
}

POWER_SANITY_DESCRIPTIONS = {
    "power_plausible": (
        "All phases report plausible power (<10 mA; "
        "analog-power jumper removed)"),
}

POWER_SANITY_THRESHOLDS = {
    "power_plausible": DEFAULT_POWER_THRESHOLD_uA,
}


def _coerce_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_power(llm_data, phase):
    """Return dict {min_uA, avg_uA, method} for a phase.

    Reads the new ``measured_min_power_uA`` / ``measured_avg_power_uA``
    fields that Gemini started returning after the min/avg split, and
    falls back to the old single ``measured_power_uA`` field (treated
    as "avg") so cached llm_results.json files from earlier runs still
    work.
    """
    empty = {"min_uA": None, "avg_uA": None, "method": None}
    item_id = _PHASE_POWER_ITEM.get(phase)
    entry = (llm_data or {}).get(item_id, {})
    if not isinstance(entry, dict):
        return empty
    min_uA = _coerce_float(entry.get("measured_min_power_uA"))
    avg_uA = _coerce_float(entry.get("measured_avg_power_uA"))
    if min_uA is None and avg_uA is None:
        legacy = _coerce_float(entry.get("measured_power_uA"))
        if legacy is not None:
            avg_uA = legacy
    return {
        "min_uA": min_uA,
        "avg_uA": avg_uA,
        "method": entry.get("measurement_method"),
    }


def _phase_power_flag(power, threshold_uA):
    """Return "ok", "high", or None for one phase's power dict.

    A phase is "high" if EITHER the min or the avg reading exceeds
    the threshold — the jumper-left-in artifact lifts both.  Returns
    None (unknown) if no numeric readings were extracted.
    """
    vals = [v for v in (power.get("min_uA"), power.get("avg_uA"))
            if v is not None]
    if not vals:
        return None
    return "high" if max(vals) > threshold_uA else "ok"


def score_student(student, video_data, llm_data):
    """Compute points for one student.

    Args:
        student:    Student name.
        video_data: dict of {phase: {score_field: value}}, or None.
        llm_data:   dict of {rubric_item_id: {verdict, reason, evidence}},
                    or None.

    Returns:
        dict with keys: student, per-item points, subtotals, grand_total,
        per-phase min/avg power, per-phase power_flag, power_sanity
        items, and power_sanity_total.
    """
    row = {"student": student}
    video_data = video_data or {}
    llm_data = llm_data or {}

    # Video rubric (per phase).
    vid_total = 0
    for phase in ("phase1", "phase2", "phase3"):
        phase_scores = video_data.get(phase, {})
        for item_id in PHASE_VIDEO_ITEMS[phase]:
            pts = PHASE_VIDEO_POINTS[phase].get(item_id, 1)
            raw = phase_scores.get(item_id, "")
            verdict = video_verdict(item_id, raw)
            earned = pts if verdict == "PASS" else 0
            row[f"video_{phase}_{item_id}"] = earned
            vid_total += earned
    row["video_total"] = vid_total

    # LLM code review rubric.
    llm_total = 0
    for item_id in LLM_RUBRIC_ITEMS:
        pts = LLM_RUBRIC_POINTS.get(item_id, 1)
        entry = llm_data.get(item_id, {})
        if isinstance(entry, dict):
            verdict = entry.get("verdict", "MISSING")
        else:
            verdict = "UNCLEAR"
        earned = pts if verdict == "PASS" else 0
        row[f"llm_{item_id}"] = earned
        llm_total += earned
    row["llm_total"] = llm_total

    # Extracted power figures (diagnostics pulled from the LLM
    # response).  Record min, avg, method, and an "ok" / "high"
    # per-phase flag using the power_plausible threshold.
    threshold = POWER_SANITY_THRESHOLDS.get(
        "power_plausible", DEFAULT_POWER_THRESHOLD_uA)
    any_high = False
    for phase in ("phase1", "phase2", "phase3"):
        power = _extract_power(llm_data, phase)
        flag = _phase_power_flag(power, threshold)
        row[f"{phase}_min_power_uA"] = power["min_uA"]
        row[f"{phase}_avg_power_uA"] = power["avg_uA"]
        row[f"{phase}_power_method"] = power["method"]
        row[f"{phase}_power_flag"] = flag
        if flag == "high":
            any_high = True

    # Computed "power_sanity" rubric.  Student earns the full
    # points if no phase exceeded the threshold; loses them if any
    # did.  If no numeric readings were extracted at all (nothing
    # flagged "high"), we give the student the points — we can't
    # prove they left the jumper in without data.
    power_total = 0
    for item_id in POWER_SANITY_ITEMS:
        max_pts = POWER_SANITY_POINTS.get(item_id, 0)
        if item_id == "power_plausible":
            earned = 0 if any_high else max_pts
        else:
            earned = max_pts
        row[f"power_{item_id}"] = earned
        power_total += earned
    row["power_sanity_total"] = power_total

    row["grand_total"] = vid_total + llm_total + power_total
    return row


def generate_grades_csv(students, video_results, llm_results, csv_path):
    """Write a grades CSV with descriptive column headers."""
    vid_max = sum(
        sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
        for p, items in PHASE_VIDEO_ITEMS.items()
    )
    llm_max = sum(LLM_RUBRIC_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)
    power_max = sum(POWER_SANITY_POINTS.get(k, 0) for k in POWER_SANITY_ITEMS)
    grand_max = vid_max + llm_max + power_max

    fieldnames = ["student"]

    # Video columns per phase.
    for phase in ("phase1", "phase2", "phase3"):
        descs = PHASE_VIDEO_DESCRIPTIONS[phase]
        pts = PHASE_VIDEO_POINTS[phase]
        for item_id in PHASE_VIDEO_ITEMS[phase]:
            desc = descs.get(item_id, item_id)
            p = pts.get(item_id, 1)
            fieldnames.append(f"video_{phase}: {desc} (max {p})")
    fieldnames.append(f"video_total (max {vid_max})")

    # LLM columns.
    for item_id in LLM_RUBRIC_ITEMS:
        desc = LLM_RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        p = LLM_RUBRIC_POINTS.get(item_id, 1)
        fieldnames.append(f"code: {desc} (max {p})")
    fieldnames.append(f"llm_total (max {llm_max})")

    # Power-sanity rubric columns (computed, not LLM-graded).
    for item_id in POWER_SANITY_ITEMS:
        desc = POWER_SANITY_DESCRIPTIONS.get(item_id, item_id)
        p = POWER_SANITY_POINTS.get(item_id, 0)
        fieldnames.append(f"power: {desc} (max {p})")
    fieldnames.append(f"power_sanity_total (max {power_max})")

    # Extracted-power diagnostic columns (numeric, not graded).
    for phase in ("phase1", "phase2", "phase3"):
        fieldnames.append(f"{phase}_min_power_uA")
        fieldnames.append(f"{phase}_avg_power_uA")
        fieldnames.append(f"{phase}_power_method")
        fieldnames.append(f"{phase}_power_flag")

    fieldnames.append(f"grand_total (max {grand_max})")

    rows = []
    for student in students:
        scored = score_student(
            student,
            video_results.get(student),
            llm_results.get(student),
        )
        csv_row = {"student": student}

        for phase in ("phase1", "phase2", "phase3"):
            descs = PHASE_VIDEO_DESCRIPTIONS[phase]
            pts = PHASE_VIDEO_POINTS[phase]
            for item_id in PHASE_VIDEO_ITEMS[phase]:
                desc = descs.get(item_id, item_id)
                p = pts.get(item_id, 1)
                csv_row[f"video_{phase}: {desc} (max {p})"] = (
                    scored[f"video_{phase}_{item_id}"])
        csv_row[f"video_total (max {vid_max})"] = scored["video_total"]

        for item_id in LLM_RUBRIC_ITEMS:
            desc = LLM_RUBRIC_DESCRIPTIONS.get(item_id, item_id)
            p = LLM_RUBRIC_POINTS.get(item_id, 1)
            csv_row[f"code: {desc} (max {p})"] = scored[f"llm_{item_id}"]
        csv_row[f"llm_total (max {llm_max})"] = scored["llm_total"]

        for item_id in POWER_SANITY_ITEMS:
            desc = POWER_SANITY_DESCRIPTIONS.get(item_id, item_id)
            p = POWER_SANITY_POINTS.get(item_id, 0)
            csv_row[f"power: {desc} (max {p})"] = scored.get(
                f"power_{item_id}", 0)
        csv_row[f"power_sanity_total (max {power_max})"] = scored.get(
            "power_sanity_total", 0)

        for phase in ("phase1", "phase2", "phase3"):
            csv_row[f"{phase}_min_power_uA"] = scored.get(
                f"{phase}_min_power_uA")
            csv_row[f"{phase}_avg_power_uA"] = scored.get(
                f"{phase}_avg_power_uA")
            csv_row[f"{phase}_power_method"] = (
                scored.get(f"{phase}_power_method"))
            csv_row[f"{phase}_power_flag"] = (
                scored.get(f"{phase}_power_flag"))

        csv_row[f"grand_total (max {grand_max})"] = scored["grand_total"]
        rows.append(csv_row)

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return rows


def generate_report(student, video_data, llm_data, report_path):
    """Write a per-student grade report text file."""
    lines = []
    lines.append("ELEC 327 - Lab 2 Grade Report")
    lines.append(f"Student: {student}")
    lines.append(f"{'=' * 60}\n")

    video_data = video_data or {}
    llm_data = llm_data or {}
    grand_total = 0

    # -- Video rubric per phase --
    for phase in ("phase1", "phase2", "phase3"):
        label = phase.replace("phase", "Phase ")
        items = PHASE_VIDEO_ITEMS[phase]
        descs = PHASE_VIDEO_DESCRIPTIONS[phase]
        pts_map = PHASE_VIDEO_POINTS[phase]
        phase_max = sum(pts_map.get(k, 1) for k in items)
        phase_scores = video_data.get(phase, {})

        lines.append(f"VIDEO ANALYSIS - {label}")
        lines.append(f"{'-' * 60}")
        lines.append(f"{'Item':<45} {'Pts':>4}  {'Max':>4}  Result")
        lines.append(f"{'-' * 60}")

        phase_total = 0
        for item_id in items:
            desc = descs.get(item_id, item_id)
            max_pts = pts_map.get(item_id, 1)
            raw = phase_scores.get(item_id, "")
            verdict = video_verdict(item_id, raw)
            earned = max_pts if verdict == "PASS" else 0
            phase_total += earned
            display = raw if raw else verdict
            lines.append(
                f"{desc:<45} {earned:>4}  {max_pts:>4}  {display}")

        lines.append(f"{'-' * 60}")
        lines.append(
            f"{label + ' VIDEO SUBTOTAL':<45} "
            f"{phase_total:>4}  {phase_max:>4}")
        grand_total += phase_total

        if phase_scores.get("error"):
            lines.append(f"  Video error: {phase_scores['error']}")
        lines.append("")

    # -- LLM code review rubric --
    llm_max = sum(LLM_RUBRIC_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)
    lines.append(f"\nCODE REVIEW RUBRIC")
    lines.append(f"{'-' * 60}")
    lines.append(f"{'Item':<45} {'Pts':>4}  {'Max':>4}  Verdict")
    lines.append(f"{'-' * 60}")

    llm_total = 0
    for item_id in LLM_RUBRIC_ITEMS:
        desc = LLM_RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        max_pts = LLM_RUBRIC_POINTS.get(item_id, 1)
        entry = llm_data.get(item_id, {})
        verdict = (entry.get("verdict", "")
                   if isinstance(entry, dict) else "UNCLEAR")
        earned = max_pts if verdict == "PASS" else 0
        llm_total += earned
        lines.append(f"{desc:<45} {earned:>4}  {max_pts:>4}  {verdict}")

    lines.append(f"{'-' * 60}")
    lines.append(f"{'CODE REVIEW SUBTOTAL':<45} {llm_total:>4}  {llm_max:>4}")
    grand_total += llm_total

    # -- Extracted power figures + power_sanity rubric --
    threshold = POWER_SANITY_THRESHOLDS.get(
        "power_plausible", DEFAULT_POWER_THRESHOLD_uA)
    power_lines = []
    any_high = False
    any_value = False
    for phase in ("phase1", "phase2", "phase3"):
        power = _extract_power(llm_data, phase)
        min_uA = power["min_uA"]
        avg_uA = power["avg_uA"]
        method = power["method"]
        if min_uA is None and avg_uA is None:
            power_lines.append(f"  {phase}: not reported")
            continue
        any_value = True
        flag = _phase_power_flag(power, threshold)
        tag = f"  [!! > {threshold / 1000:.0f} mA]" if flag == "high" else ""
        if flag == "high":
            any_high = True
        m = f" ({method})" if method else ""
        min_s = f"{min_uA:>8.1f}" if min_uA is not None else "     n/a"
        avg_s = f"{avg_uA:>8.1f}" if avg_uA is not None else "     n/a"
        power_lines.append(
            f"  {phase}:  min={min_s} µA   avg={avg_s} µA{m}{tag}")

    if any_value:
        lines.append(f"\nEXTRACTED POWER FIGURES (from writeup)")
        lines.append(f"{'-' * 60}")
        lines.extend(power_lines)
        if any_high:
            lines.append("")
            lines.append(
                f"  WARNING: at least one reading exceeds "
                f"{threshold / 1000:.0f} mA.")
            lines.append(
                "  This almost always means the LaunchPad analog-power")
            lines.append(
                "  jumper (3V3 ANA / J101) was left in place, so the")
            lines.append(
                "  EnergyTrace reading also includes the XDS debug probe")
            lines.append(
                "  supply and the measured figures are over-estimates.")

    power_max = sum(POWER_SANITY_POINTS.get(k, 0) for k in POWER_SANITY_ITEMS)
    if power_max > 0:
        lines.append(f"\nPOWER SANITY RUBRIC")
        lines.append(f"{'-' * 60}")
        lines.append(f"{'Item':<45} {'Pts':>4}  {'Max':>4}  Result")
        lines.append(f"{'-' * 60}")
        power_total = 0
        for item_id in POWER_SANITY_ITEMS:
            desc = POWER_SANITY_DESCRIPTIONS.get(item_id, item_id)
            max_pts = POWER_SANITY_POINTS.get(item_id, 0)
            if item_id == "power_plausible":
                result = "FAIL" if any_high else "PASS"
            else:
                result = "PASS"
            earned = max_pts if result == "PASS" else 0
            power_total += earned
            lines.append(
                f"{desc:<45} {earned:>4}  {max_pts:>4}  {result}")
        lines.append(f"{'-' * 60}")
        lines.append(
            f"{'POWER SANITY SUBTOTAL':<45} "
            f"{power_total:>4}  {power_max:>4}")
        grand_total += power_total

    # -- Grand total --
    vid_max = sum(
        sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
        for p, items in PHASE_VIDEO_ITEMS.items()
    )
    total_max = vid_max + llm_max + power_max
    lines.append(f"\n{'=' * 60}")
    lines.append(f"{'TOTAL':<45} {grand_total:>4}  {total_max:>4}")
    lines.append(f"{'=' * 60}")

    # -- Detailed LLM findings --
    lines.append(f"\n\nDETAILED CODE REVIEW FINDINGS")
    lines.append(f"{'=' * 60}")
    for item_id in LLM_RUBRIC_ITEMS:
        desc = LLM_RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        entry = llm_data.get(item_id, {})
        if isinstance(entry, dict):
            verdict = entry.get("verdict", "")
            reason = entry.get("reason", "")
            evidence = entry.get("evidence", "")
        else:
            verdict, reason, evidence = "UNCLEAR", str(entry), ""

        lines.append(f"\n{desc}")
        lines.append(f"  Verdict: {verdict}")
        if reason:
            lines.append(f"  Reason:  {reason}")
        if evidence:
            lines.append(f"  Evidence:")
            for eline in str(evidence).split("\n"):
                lines.append(f"    > {eline}")

    lines.append("")

    with open(report_path, "w") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(
        description="Score Lab 2 analysis results and generate "
                    "grades/reports")
    parser.add_argument(
        "--export-rubric", metavar="FILE",
        help="Export default rubric YAML for editing, then exit")
    parser.add_argument(
        "--rubric", metavar="FILE",
        help="Rubric YAML with point weights")
    parser.add_argument(
        "--video-results", metavar="FILE",
        help="Path to video_results.json (from grade.py)")
    parser.add_argument(
        "--llm-results", metavar="FILE",
        help="Path to llm_results.json (from grade.py)")
    parser.add_argument(
        "--grades-csv", metavar="FILE", default="grades.csv",
        help="Output grades CSV path (default: grades.csv)")
    parser.add_argument(
        "--reports-dir", metavar="DIR",
        help="Generate per-student text grade reports in DIR")

    args = parser.parse_args()

    # -- Export rubric --
    if args.export_rubric:
        export_rubric(args.export_rubric)
        print(f"Rubric exported to {args.export_rubric}")
        print("Edit the 'points' values, then pass --rubric to use them.")
        sys.exit(0)

    # -- Load rubric weights --
    if args.rubric:
        vid_max, llm_max, power_max = load_rubric(args.rubric)
        print(f"Loaded rubric from {args.rubric} "
              f"(video: {vid_max} pts, code: {llm_max} pts, "
              f"power: {power_max} pts)")
    else:
        vid_max = sum(
            sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
            for p, items in PHASE_VIDEO_ITEMS.items()
        )
        llm_max = sum(LLM_RUBRIC_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)
        power_max = sum(POWER_SANITY_POINTS.get(k, 0)
                        for k in POWER_SANITY_ITEMS)

    # -- Load results --
    video_results = {}
    if args.video_results:
        with open(args.video_results, "r") as f:
            video_results = json.load(f)
        print(f"Video results: {len(video_results)} raw keys")
        video_results = _normalize_video_results(video_results)
        print(f"Video results: {len(video_results)} students after merge")

    llm_results = {}
    if args.llm_results:
        with open(args.llm_results, "r") as f:
            llm_results = json.load(f)
        print(f"LLM results: {len(llm_results)} raw keys")
        llm_results = _normalize_llm_results(llm_results)
        print(f"LLM results: {len(llm_results)} students after merge")

    if not video_results and not llm_results:
        print("Error: provide --video-results and/or --llm-results")
        sys.exit(1)

    all_students = sorted(set(video_results) | set(llm_results))
    print(f"Total students: {len(all_students)}")

    # -- Generate grades CSV --
    rows = generate_grades_csv(
        all_students, video_results, llm_results, args.grades_csv)
    print(f"\nGrades written to {args.grades_csv}")

    grand_max = vid_max + llm_max + power_max
    totals = [r.get(f"grand_total (max {grand_max})", 0)
              for r in rows]
    if totals:
        avg = sum(totals) / len(totals)
        print(f"  Average: {avg:.1f}/{grand_max}")

    # -- Generate reports --
    if args.reports_dir:
        os.makedirs(args.reports_dir, exist_ok=True)
        for student in all_students:
            report_path = os.path.join(
                args.reports_dir, f"{student}_report.txt")
            generate_report(
                student,
                video_results.get(student),
                llm_results.get(student),
                report_path,
            )
        print(f"Reports written to {args.reports_dir}/ "
              f"({len(all_students)} files)")


if __name__ == "__main__":
    main()
