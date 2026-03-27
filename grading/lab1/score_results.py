"""
Score raw analysis results and generate grades CSV and student reports.

Reads video_results.json and/or llm_results.json (produced by grade.py),
applies rubric point weights from a YAML file, and outputs:
  - A simple grades CSV (student, per-item points, subtotals, grand total)
  - Optional per-student text grade reports

Usage:
    # Export default rubric for editing:
    python -m grading.lab1.score_results --export-rubric rubric.yaml

    # Score results:
    python -m grading.lab1.score_results \
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

from assess.lab1_score import (
    SCORE_FIELDS,
    VIDEO_RUBRIC_ITEMS, VIDEO_RUBRIC_POINTS, VIDEO_RUBRIC_DESCRIPTIONS,
    VIDEO_RUBRIC_MAX_POINTS, video_verdict,
)
from assess.code_review import (
    RUBRIC_ITEMS,
    RUBRIC_POINTS, RUBRIC_DESCRIPTIONS, RUBRIC_MAX_POINTS,
)


def load_rubric(rubric_path):
    """Load rubric weights from YAML, updating the module-level dicts.

    Returns (video_max_points, code_max_points).
    """
    with open(rubric_path, "r") as f:
        data = yaml.safe_load(f)

    for entry in data.get("video_rubric", []):
        item_id = entry["id"]
        if item_id in VIDEO_RUBRIC_POINTS:
            VIDEO_RUBRIC_POINTS[item_id] = entry["points"]
            if "description" in entry:
                VIDEO_RUBRIC_DESCRIPTIONS[item_id] = entry["description"]

    code_entries = data.get("code_rubric", []) or data.get("rubric", [])
    for entry in code_entries:
        item_id = entry["id"]
        if item_id in RUBRIC_POINTS:
            RUBRIC_POINTS[item_id] = entry["points"]
            if "description" in entry:
                RUBRIC_DESCRIPTIONS[item_id] = entry["description"]

    import grading.lab1.score as _sc
    import grading.lab1.code_review as _cr
    _sc.VIDEO_RUBRIC_MAX_POINTS = sum(
        VIDEO_RUBRIC_POINTS.get(k, 1) for k in VIDEO_RUBRIC_ITEMS)
    _cr.RUBRIC_MAX_POINTS = sum(
        RUBRIC_POINTS.get(k, 1) for k in RUBRIC_ITEMS)
    return _sc.VIDEO_RUBRIC_MAX_POINTS, _cr.RUBRIC_MAX_POINTS


def export_rubric(path):
    """Write default rubric YAML for user editing."""
    video_items = [
        {"id": k,
         "description": VIDEO_RUBRIC_DESCRIPTIONS.get(k, k),
         "points": VIDEO_RUBRIC_POINTS.get(k, 1)}
        for k in VIDEO_RUBRIC_ITEMS
    ]
    code_items = [
        {"id": k,
         "description": RUBRIC_DESCRIPTIONS.get(k, k),
         "points": RUBRIC_POINTS.get(k, 1)}
        for k in RUBRIC_ITEMS
    ]
    with open(path, "w") as f:
        yaml.dump({"video_rubric": video_items,
                    "code_rubric": code_items}, f,
                  default_flow_style=False, sort_keys=False)


def score_student(student, video_data, llm_data):
    """Compute points for one student.

    Args:
        student:    Student name.
        video_data: dict of SCORE_FIELDS values (from video_results.json),
                    or None if no video data.
        llm_data:   dict of rubric item results (from llm_results.json),
                    or None if no LLM data.

    Returns:
        dict with keys: student, per-item points, subtotals, grand_total.
    """
    row = {"student": student}
    vid_max = sum(VIDEO_RUBRIC_POINTS.get(k, 1) for k in VIDEO_RUBRIC_ITEMS)
    llm_max = sum(RUBRIC_POINTS.get(k, 1) for k in RUBRIC_ITEMS)

    # Video rubric
    vid_total = 0
    for item_id in VIDEO_RUBRIC_ITEMS:
        pts = VIDEO_RUBRIC_POINTS.get(item_id, 1)
        raw = (video_data or {}).get(item_id, "")
        verdict = video_verdict(item_id, raw)
        earned = pts if verdict == "PASS" else 0
        row[f"video_{item_id}"] = earned
        vid_total += earned
    row["video_total"] = vid_total

    # LLM code review rubric
    llm_total = 0
    for item_id in RUBRIC_ITEMS:
        pts = RUBRIC_POINTS.get(item_id, 1)
        entry = (llm_data or {}).get(item_id, {})
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
    """Write a simple grades CSV."""
    vid_max = sum(VIDEO_RUBRIC_POINTS.get(k, 1) for k in VIDEO_RUBRIC_ITEMS)
    llm_max = sum(RUBRIC_POINTS.get(k, 1) for k in RUBRIC_ITEMS)
    grand_max = vid_max + llm_max

    fieldnames = ["student"]
    for item_id in VIDEO_RUBRIC_ITEMS:
        desc = VIDEO_RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        pts = VIDEO_RUBRIC_POINTS.get(item_id, 1)
        fieldnames.append(f"video: {desc} (max {pts})")
    fieldnames.append(f"video_total (max {vid_max})")
    for item_id in RUBRIC_ITEMS:
        desc = RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        pts = RUBRIC_POINTS.get(item_id, 1)
        fieldnames.append(f"code: {desc} (max {pts})")
    fieldnames.append(f"llm_total (max {llm_max})")
    fieldnames.append(f"grand_total (max {grand_max})")

    rows = []
    for student in students:
        scored = score_student(
            student,
            video_results.get(student),
            llm_results.get(student),
        )
        # Map to descriptive column names
        csv_row = {"student": student}
        for item_id in VIDEO_RUBRIC_ITEMS:
            desc = VIDEO_RUBRIC_DESCRIPTIONS.get(item_id, item_id)
            pts = VIDEO_RUBRIC_POINTS.get(item_id, 1)
            csv_row[f"video: {desc} (max {pts})"] = scored[f"video_{item_id}"]
        csv_row[f"video_total (max {vid_max})"] = scored["video_total"]
        for item_id in RUBRIC_ITEMS:
            desc = RUBRIC_DESCRIPTIONS.get(item_id, item_id)
            pts = RUBRIC_POINTS.get(item_id, 1)
            csv_row[f"code: {desc} (max {pts})"] = scored[f"llm_{item_id}"]
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
    lines.append("ELEC 327 — Lab 1 Grade Report")
    lines.append(f"Student: {student}")
    lines.append(f"{'=' * 60}\n")

    vid_max = sum(VIDEO_RUBRIC_POINTS.get(k, 1) for k in VIDEO_RUBRIC_ITEMS)
    llm_max = sum(RUBRIC_POINTS.get(k, 1) for k in RUBRIC_ITEMS)
    grand_total = 0

    # ── Video rubric ──
    lines.append("VIDEO ANALYSIS RUBRIC")
    lines.append(f"{'-' * 60}")
    lines.append(f"{'Item':<45} {'Pts':>4}  {'Max':>4}  Result")
    lines.append(f"{'-' * 60}")

    vid_total = 0
    video = video_data or {}
    for item_id in VIDEO_RUBRIC_ITEMS:
        desc = VIDEO_RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        max_pts = VIDEO_RUBRIC_POINTS.get(item_id, 1)
        raw = video.get(item_id, "")
        verdict = video_verdict(item_id, raw)
        earned = max_pts if verdict == "PASS" else 0
        vid_total += earned
        display = raw if raw else verdict
        lines.append(f"{desc:<45} {earned:>4}  {max_pts:>4}  {display}")

    lines.append(f"{'-' * 60}")
    lines.append(f"{'VIDEO SUBTOTAL':<45} {vid_total:>4}  {vid_max:>4}")
    grand_total += vid_total

    # Video measurements
    measurement_fields = [k for k in SCORE_FIELDS if k not in VIDEO_RUBRIC_ITEMS]
    measurements = [(k, video.get(k, "")) for k in measurement_fields
                    if video.get(k, "")]
    if measurements:
        lines.append("")
        lines.append("  Additional measurements:")
        for k, v in measurements:
            label = k.replace("_", " ").title()
            lines.append(f"    {label:<40} {v}")
    if video.get("error"):
        lines.append(f"  Video error: {video['error']}")

    # ── LLM code review rubric ──
    lines.append(f"\n\nCODE REVIEW RUBRIC")
    lines.append(f"{'-' * 60}")
    lines.append(f"{'Item':<45} {'Pts':>4}  {'Max':>4}  Verdict")
    lines.append(f"{'-' * 60}")

    llm = llm_data or {}
    llm_total = 0
    for item_id in RUBRIC_ITEMS:
        desc = RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        max_pts = RUBRIC_POINTS.get(item_id, 1)
        entry = llm.get(item_id, {})
        verdict = entry.get("verdict", "") if isinstance(entry, dict) else "UNCLEAR"
        earned = max_pts if verdict == "PASS" else 0
        llm_total += earned
        lines.append(f"{desc:<45} {earned:>4}  {max_pts:>4}  {verdict}")

    lines.append(f"{'-' * 60}")
    lines.append(f"{'CODE REVIEW SUBTOTAL':<45} {llm_total:>4}  {llm_max:>4}")
    grand_total += llm_total

    # ── Grand total ──
    lines.append(f"\n{'=' * 60}")
    lines.append(f"{'TOTAL':<45} {grand_total:>4}  {vid_max + llm_max:>4}")
    lines.append(f"{'=' * 60}")

    # ── Detailed LLM findings ──
    lines.append(f"\n\nDETAILED CODE REVIEW FINDINGS")
    lines.append(f"{'=' * 60}")
    for item_id in RUBRIC_ITEMS:
        desc = RUBRIC_DESCRIPTIONS.get(item_id, item_id)
        entry = llm.get(item_id, {})
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
        description="Score analysis results and generate grades/reports")
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

    # ── Export rubric ──
    if args.export_rubric:
        export_rubric(args.export_rubric)
        print(f"Rubric exported to {args.export_rubric}")
        print("Edit the 'points' values, then pass --rubric to use them.")
        sys.exit(0)

    # ── Load rubric weights ──
    if args.rubric:
        vid_max, llm_max = load_rubric(args.rubric)
        print(f"Loaded rubric from {args.rubric} "
              f"(video: {vid_max} pts, code: {llm_max} pts)")
    else:
        vid_max = sum(VIDEO_RUBRIC_POINTS.get(k, 1) for k in VIDEO_RUBRIC_ITEMS)
        llm_max = sum(RUBRIC_POINTS.get(k, 1) for k in RUBRIC_ITEMS)

    # ── Load results ──
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

    # All students from both sources.
    all_students = sorted(set(video_results) | set(llm_results))
    print(f"Total students: {len(all_students)}")

    # ── Generate grades CSV ──
    rows = generate_grades_csv(
        all_students, video_results, llm_results, args.grades_csv)
    print(f"\nGrades written to {args.grades_csv}")

    # Print summary.
    totals = [r.get(f"grand_total (max {vid_max + llm_max})", 0)
              for r in rows]
    if totals:
        avg = sum(totals) / len(totals)
        print(f"  Average: {avg:.1f}/{vid_max + llm_max}")

    # ── Generate reports ──
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
