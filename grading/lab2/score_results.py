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

    Returns (total_video_max, total_llm_max).
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

    vid_max = sum(
        sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
        for p, items in PHASE_VIDEO_ITEMS.items()
    )
    llm_max = sum(LLM_RUBRIC_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)
    return vid_max, llm_max


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
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def score_student(student, video_data, llm_data):
    """Compute points for one student.

    Args:
        student:    Student name.
        video_data: dict of {phase: {score_field: value}}, or None.
        llm_data:   dict of {rubric_item_id: {verdict, reason, evidence}},
                    or None.

    Returns:
        dict with keys: student, per-item points, subtotals, grand_total.
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

    row["grand_total"] = vid_total + llm_total
    return row


def generate_grades_csv(students, video_results, llm_results, csv_path):
    """Write a grades CSV with descriptive column headers."""
    vid_max = sum(
        sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
        for p, items in PHASE_VIDEO_ITEMS.items()
    )
    llm_max = sum(LLM_RUBRIC_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)
    grand_max = vid_max + llm_max

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

    # -- Grand total --
    vid_max = sum(
        sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
        for p, items in PHASE_VIDEO_ITEMS.items()
    )
    total_max = vid_max + llm_max
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
        vid_max, llm_max = load_rubric(args.rubric)
        print(f"Loaded rubric from {args.rubric} "
              f"(video: {vid_max} pts, code: {llm_max} pts)")
    else:
        vid_max = sum(
            sum(PHASE_VIDEO_POINTS[p].get(k, 1) for k in items)
            for p, items in PHASE_VIDEO_ITEMS.items()
        )
        llm_max = sum(LLM_RUBRIC_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)

    # -- Load results --
    video_results = {}
    if args.video_results:
        with open(args.video_results, "r") as f:
            video_results = json.load(f)
        print(f"Video results: {len(video_results)} students")

    llm_results = {}
    if args.llm_results:
        with open(args.llm_results, "r") as f:
            llm_results = json.load(f)
        print(f"LLM results: {len(llm_results)} students")

    if not video_results and not llm_results:
        print("Error: provide --video-results and/or --llm-results")
        sys.exit(1)

    all_students = sorted(set(video_results) | set(llm_results))
    print(f"Total students: {len(all_students)}")

    # -- Generate grades CSV --
    rows = generate_grades_csv(
        all_students, video_results, llm_results, args.grades_csv)
    print(f"\nGrades written to {args.grades_csv}")

    totals = [r.get(f"grand_total (max {vid_max + llm_max})", 0)
              for r in rows]
    if totals:
        avg = sum(totals) / len(totals)
        print(f"  Average: {avg:.1f}/{vid_max + llm_max}")

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
