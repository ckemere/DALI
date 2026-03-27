#!/usr/bin/env python3
"""
Score Lab 4 (PCB Design) submissions and generate a Canvas-ready grades CSV.

Combines two data sources:
  1. PCB analysis CSV from grading/lab4/grade_pcbs.py (area, initials, DRC results)
  2. Pre-submission review timestamps from grading/fetch_submission_times.py

Rubric (100 + 10 bonus):
  70 pts  — Submitted a PCB design
  10 pts  — Board area ≤ maximum (default 1291 mm²)
  10 pts  — Has initials on copper layer
  10 pts  — No errors in weak DRC
  10 pts  — (bonus) Pre-submission review submitted before cutoff

Usage:
    python -m grading.lab4.score \\
        --pcb-csv pcb_results.csv \\
        --presubmit-csv lab4a_times.csv \\
        --cutoff "2025-03-15T14:15:00-05:00" \\
        -o lab4_grades.csv
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CENTRAL_TZ = ZoneInfo("America/Chicago")


def _fmt_central(dt):
    """Format a datetime as a readable Central Time string."""
    ct = dt.astimezone(CENTRAL_TZ)
    return ct.strftime("%b %d %I:%M %p %Z")


def parse_cutoff(cutoff_str):
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime."""
    return datetime.fromisoformat(cutoff_str)


def load_pcb_results(path):
    """Load PCB analysis CSV output, keyed by net_id."""
    results = {}
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            net_id = row["net_id"].strip().lower()
            results[net_id] = row
    return results


def load_presubmit_times(path):
    """Load pre-submission times CSV, keyed by net_id."""
    times = {}
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            net_id = row["net_id"].strip().lower()
            times[net_id] = row
    return times


def is_on_time(submitted_at_str, cutoff):
    """Check whether a submission timestamp is before the cutoff."""
    if not submitted_at_str:
        return False
    submitted = datetime.fromisoformat(submitted_at_str)
    # Canvas timestamps are UTC (ending in Z); fromisoformat handles this
    # in Python 3.11+. For older Python, replace trailing Z.
    if submitted.tzinfo is None:
        submitted = submitted.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return submitted <= cutoff


def score_student(pcb_row, presubmit_row, max_area, cutoff):
    """
    Compute score and rubric breakdown for one student.

    Returns (total_score, rubric_text, rubric_dict).
    """
    rubric = {}

    # 70 pts: submitted something (if they have a PCB row, they submitted)
    rubric["submission"] = 70

    # 10 pts: board area ≤ max_area
    area = 0.0
    try:
        area = float(pcb_row.get("area_mm2", 0))
    except (ValueError, TypeError):
        pass

    if area > 0 and area <= max_area:
        rubric["area"] = 10
    else:
        rubric["area"] = 0

    # 10 pts: has initials on copper
    copper_texts = pcb_row.get("copper_texts", "").strip()
    if copper_texts:
        rubric["initials"] = 10
    else:
        rubric["initials"] = 0

    # 10 pts: weak DRC pass
    weak_pass = pcb_row.get("weak_drc_pass", "").strip()
    if weak_pass == "True":
        rubric["weak_drc"] = 10
    else:
        rubric["weak_drc"] = 0

    # 10 pts bonus: pre-submission review on time
    presubmit_submitted_at = None
    if presubmit_row and cutoff:
        submitted_at_str = presubmit_row.get("submitted_at", "")
        if submitted_at_str:
            presubmit_submitted_at = datetime.fromisoformat(submitted_at_str)
            if presubmit_submitted_at.tzinfo is None:
                presubmit_submitted_at = presubmit_submitted_at.replace(
                    tzinfo=timezone.utc)
        if is_on_time(submitted_at_str, cutoff):
            rubric["presubmit_bonus"] = 10
        else:
            rubric["presubmit_bonus"] = 0
    else:
        rubric["presubmit_bonus"] = 0

    total = sum(rubric.values())

    # Build human-readable rubric text
    lines = []
    lines.append(f"Lab 4 PCB Grade: {total}/100")
    lines.append("")
    lines.append(f"[{rubric['submission']:2d}/70] Submission received")

    if rubric["area"] > 0:
        lines.append(f"[{rubric['area']:2d}/10] Board area: {area:.0f} mm² (≤ {max_area:.0f} mm²)")
    else:
        if area > 0:
            lines.append(f"[ 0/10] Board area: {area:.0f} mm² (exceeds {max_area:.0f} mm²)")
        else:
            lines.append(f"[ 0/10] Board area: could not be determined")

    if rubric["initials"] > 0:
        lines.append(f"[{rubric['initials']:2d}/10] Initials found on copper: \"{copper_texts}\"")
    else:
        lines.append(f"[ 0/10] No initials found on copper layer")

    weak_errors = pcb_row.get("weak_drc_errors", "")
    if rubric["weak_drc"] > 0:
        lines.append(f"[{rubric['weak_drc']:2d}/10] Weak DRC: PASS")
    else:
        if weak_errors:
            lines.append(f"[ 0/10] Weak DRC: FAIL ({weak_errors} errors)")
        else:
            lines.append(f"[ 0/10] Weak DRC: FAIL")

    if rubric["presubmit_bonus"] > 0:
        lines.append(
            f"[{rubric['presubmit_bonus']:2d}/10] BONUS: Pre-submission review on time"
            f" (submitted {_fmt_central(presubmit_submitted_at)},"
            f" deadline {_fmt_central(cutoff)})")
    elif presubmit_submitted_at and cutoff:
        lines.append(
            f"[ 0/10] BONUS: Pre-submission review late"
            f" (submitted {_fmt_central(presubmit_submitted_at)},"
            f" deadline {_fmt_central(cutoff)})")
    elif cutoff:
        lines.append(
            f"[ 0/10] BONUS: No pre-submission review received"
            f" (deadline was {_fmt_central(cutoff)})")
    else:
        lines.append(f"[ 0/10] BONUS: Pre-submission review not evaluated")

    rubric_text = "\n".join(lines)
    return total, rubric_text, rubric


def main():
    parser = argparse.ArgumentParser(
        description="Score Lab 4 PCB submissions and generate Canvas-ready CSV.")
    parser.add_argument(
        "--pcb-csv", required=True,
        help="CSV from grading.lab4.grade_pcbs")
    parser.add_argument(
        "--presubmit-csv", default=None,
        help="CSV from grading.fetch_submission_times (Lab 4A submissions)")
    parser.add_argument(
        "--cutoff", default=None,
        help="ISO 8601 cutoff time for pre-submission bonus "
             "(e.g. '2025-03-15T14:15:00-05:00')")
    parser.add_argument(
        "--max-area", type=float, default=1291.0,
        help="Maximum board area in mm² for full marks (default: 1291)")
    parser.add_argument(
        "-o", "--output", default="lab4_grades.csv",
        help="Output CSV path (default: lab4_grades.csv)")
    parser.add_argument(
        "--canvas-score-column", default="score",
        help="Name of the score column in output CSV (default: score)")

    args = parser.parse_args()

    # Parse cutoff
    cutoff = None
    if args.cutoff:
        try:
            cutoff = parse_cutoff(args.cutoff)
        except ValueError as e:
            sys.exit(f"ERROR: Invalid cutoff timestamp: {e}")
    elif args.presubmit_csv:
        print("WARNING: --presubmit-csv provided without --cutoff; "
              "bonus points will not be awarded", file=sys.stderr)

    # Load data
    print(f"Loading PCB results from {args.pcb_csv}...", file=sys.stderr)
    pcb_data = load_pcb_results(args.pcb_csv)
    print(f"  {len(pcb_data)} students", file=sys.stderr)

    presubmit_data = {}
    if args.presubmit_csv:
        print(f"Loading pre-submission times from {args.presubmit_csv}...",
              file=sys.stderr)
        presubmit_data = load_presubmit_times(args.presubmit_csv)
        print(f"  {len(presubmit_data)} submissions", file=sys.stderr)
        if cutoff:
            on_time = sum(
                1 for r in presubmit_data.values()
                if is_on_time(r.get("submitted_at", ""), cutoff)
            )
            print(f"  {on_time} on time (before {cutoff.isoformat()})",
                  file=sys.stderr)

    # Score each student
    fieldnames = [
        "student_name", "net_id",
        "pts_submission", "pts_area", "pts_initials", "pts_weak_drc",
        "pts_presubmit_bonus", args.canvas_score_column, "rubric_text",
    ]

    rows = []
    for net_id, pcb_row in sorted(pcb_data.items()):
        presubmit_row = presubmit_data.get(net_id)
        total, rubric_text, rubric = score_student(
            pcb_row, presubmit_row, args.max_area, cutoff)

        rows.append({
            "student_name": pcb_row.get("student_name", ""),
            "net_id": net_id,
            "pts_submission": rubric["submission"],
            "pts_area": rubric["area"],
            "pts_initials": rubric["initials"],
            "pts_weak_drc": rubric["weak_drc"],
            "pts_presubmit_bonus": rubric["presubmit_bonus"],
            args.canvas_score_column: total,
            "rubric_text": rubric_text,
        })

    # Write output CSV
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} grades to {args.output}", file=sys.stderr)

    # Summary
    scores = [r[args.canvas_score_column] for r in rows]
    if scores:
        avg = sum(scores) / len(scores)
        perfect = sum(1 for s in scores if s >= 100)
        bonus = sum(1 for r in rows if r["pts_presubmit_bonus"] > 0)
        print(f"  Average: {avg:.1f}", file=sys.stderr)
        print(f"  Full marks (≥100): {perfect}/{len(rows)}", file=sys.stderr)
        print(f"  Pre-submit bonus: {bonus}/{len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
