"""Score raw analysis results and generate grades CSV and student reports
for Lab 3.

Reads video_results.json and/or llm_results.json, applies rubric point
weights from a YAML file, and outputs:
  - A simple grades CSV (student, per-item points, subtotals, grand total)
  - Optional per-student text grade reports

Usage:
    # Export default rubric for editing:
    python -m grading.lab3.score_results --export-rubric rubric.yaml

    # Score results:
    python -m grading.lab3.score_results \\
        --video-results video_results.json \\
        --llm-results llm_results.json \\
        --rubric rubric.yaml \\
        --grades-csv grades.csv \\
        --reports-dir reports/
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

import yaml

from assess.build import student_name_from_zip
from assess.lab3_score import (
    ALL_LLM_DESCRIPTIONS,
    ALL_LLM_ITEMS,
    ALL_LLM_POINTS,
    ALL_VIDEO_DESCRIPTIONS,
    ALL_VIDEO_ITEMS,
    ALL_VIDEO_POINTS,
    ALTERNATIVE_ITEMS,
    EC_LLM_RUBRIC_ITEMS,
    EC_VIDEO_RUBRIC_ITEMS,
    LLM_RUBRIC_ITEMS,
    VIDEO_RUBRIC_ITEMS,
    video_verdict,
)


# =====================================================================
# Rubric YAML export / import
# =====================================================================


def export_rubric(path: str) -> None:
    """Write default rubric YAML for instructor editing."""
    data: Dict[str, Any] = {}

    data["video_rubric"] = [
        {
            "id": k,
            "description": ALL_VIDEO_DESCRIPTIONS.get(k, k),
            "points": ALL_VIDEO_POINTS.get(k, 1),
            "category": "ec" if k in EC_VIDEO_RUBRIC_ITEMS else "base",
        }
        for k in ALL_VIDEO_ITEMS
    ]

    data["llm_rubric"] = [
        {
            "id": k,
            "description": ALL_LLM_DESCRIPTIONS.get(k, k),
            "points": ALL_LLM_POINTS.get(k, 1),
            "category": "ec" if k in EC_LLM_RUBRIC_ITEMS else "base",
        }
        for k in ALL_LLM_ITEMS
    ]

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    print(f"Default rubric written to {path}")

    vid_base = sum(
        ALL_VIDEO_POINTS.get(k, 1) for k in VIDEO_RUBRIC_ITEMS)
    vid_ec = sum(
        ALL_VIDEO_POINTS.get(k, 1) for k in EC_VIDEO_RUBRIC_ITEMS)
    llm_base = sum(
        ALL_LLM_POINTS.get(k, 1) for k in LLM_RUBRIC_ITEMS)
    llm_ec = sum(
        ALL_LLM_POINTS.get(k, 1) for k in EC_LLM_RUBRIC_ITEMS)
    print(f"  Video: {vid_base} base + {vid_ec} EC = {vid_base + vid_ec}")
    print(f"  LLM:   {llm_base} base + {llm_ec} EC = {llm_base + llm_ec}")
    print(f"  Total: {vid_base + llm_base} base + {vid_ec + llm_ec} EC "
          f"= {vid_base + vid_ec + llm_base + llm_ec}")


def load_rubric(rubric_path: str) -> None:
    """Load rubric weights from YAML, updating the module-level dicts."""
    with open(rubric_path, "r") as f:
        data = yaml.safe_load(f)

    for entry in data.get("video_rubric", []):
        item_id = entry["id"]
        if item_id in ALL_VIDEO_POINTS:
            ALL_VIDEO_POINTS[item_id] = entry["points"]
            if "description" in entry:
                ALL_VIDEO_DESCRIPTIONS[item_id] = entry["description"]

    for entry in data.get("llm_rubric", []):
        item_id = entry["id"]
        if item_id in ALL_LLM_POINTS:
            ALL_LLM_POINTS[item_id] = entry["points"]
            if "description" in entry:
                ALL_LLM_DESCRIPTIONS[item_id] = entry["description"]


# =====================================================================
# Scoring helpers
# =====================================================================


def _normalize_results(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse Canvas-wrapped student keys to canonical names."""
    merged: Dict[str, Any] = {}
    for raw_key in sorted(raw.keys()):
        canon = student_name_from_zip(raw_key)
        existing = merged.get(canon)
        entry = raw[raw_key]
        if existing is None:
            merged[canon] = entry
        elif isinstance(existing, dict) and isinstance(entry, dict):
            # Merge, later keys win.
            existing.update(entry)
    if len(merged) < len(raw):
        print(f"  Normalized: {len(raw)} raw keys -> {len(merged)} students")
    return merged


def score_video_item(
    video_data: Dict[str, Any],
    item: str,
) -> str:
    """Return PASS / FAIL / NO_DATA for a single video rubric item.

    Checks the primary item, and if it fails, checks
    ``ALTERNATIVE_ITEMS`` for a backup.
    """
    primary = video_verdict(video_data.get(item))
    if primary == "PASS":
        return "PASS"
    alt_key = ALTERNATIVE_ITEMS.get(item)
    if alt_key:
        alt = video_verdict(video_data.get(alt_key))
        if alt == "PASS":
            return "PASS"
    return primary


def score_llm_item(
    llm_data: Dict[str, Any],
    item: str,
) -> str:
    """Return PASS / FAIL / NO_DATA for a single LLM rubric item."""
    entry = llm_data.get(item, {})
    if isinstance(entry, dict):
        v = str(entry.get("verdict", "")).upper()
    else:
        v = str(entry).upper()
    if v.startswith("PASS"):
        return "PASS"
    if v.startswith("FAIL"):
        return "FAIL"
    return "NO_DATA"


def score_student(
    video_data: Optional[Dict[str, Any]],
    llm_data: Optional[Dict[str, Any]],
    include_ec: bool = True,
) -> Dict[str, Any]:
    """Score a single student across all rubric items.

    Returns a dict with per-item verdicts, subtotals, and grand total.
    """
    result: Dict[str, Any] = {}
    vid = video_data or {}
    llm = llm_data or {}

    # Video items
    video_items = list(ALL_VIDEO_ITEMS) if include_ec else list(VIDEO_RUBRIC_ITEMS)
    video_total = 0
    video_max = 0
    for item in video_items:
        verdict = score_video_item(vid, item)
        pts = ALL_VIDEO_POINTS.get(item, 1) if verdict == "PASS" else 0
        max_pts = ALL_VIDEO_POINTS.get(item, 1)
        result[f"video_{item}"] = verdict
        result[f"video_{item}_pts"] = pts
        video_total += pts
        video_max += max_pts

    result["video_total"] = video_total
    result["video_max"] = video_max

    # LLM items
    llm_items = list(ALL_LLM_ITEMS) if include_ec else list(LLM_RUBRIC_ITEMS)
    llm_total = 0
    llm_max = 0
    for item in llm_items:
        verdict = score_llm_item(llm, item)
        pts = ALL_LLM_POINTS.get(item, 1) if verdict == "PASS" else 0
        max_pts = ALL_LLM_POINTS.get(item, 1)
        result[f"llm_{item}"] = verdict
        result[f"llm_{item}_pts"] = pts
        llm_total += pts
        llm_max += max_pts

    result["llm_total"] = llm_total
    result["llm_max"] = llm_max

    result["grand_total"] = video_total + llm_total
    result["grand_max"] = video_max + llm_max

    return result


# =====================================================================
# Batch scoring
# =====================================================================


def load_video_results_csv(csv_path: str) -> Dict[str, Dict[str, Any]]:
    """Read a video results CSV into {student: {item: verdict, ...}}.

    The CSV has columns: student, <item>, <item>_detail, ...
    This extracts only the verdict columns (not _detail).
    """
    results: Dict[str, Dict[str, Any]] = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            student = row.get("student", "").strip()
            if not student:
                continue
            data: Dict[str, Any] = {}
            for key, val in row.items():
                if key == "student" or key.endswith("_detail"):
                    continue
                if val:
                    data[key] = val
            results[student] = data
    return results


def generate_grades(
    video_results: Optional[Dict[str, Any]],
    llm_results: Optional[Dict[str, Any]],
    include_ec: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Score all students, returning ``{student: score_dict}``."""
    vid = _normalize_results(video_results) if video_results else {}
    llm = _normalize_results(llm_results) if llm_results else {}

    all_students = sorted(set(vid.keys()) | set(llm.keys()))
    out: Dict[str, Dict[str, Any]] = {}
    for s in all_students:
        out[s] = score_student(vid.get(s), llm.get(s), include_ec=include_ec)
    return out


def write_grades_csv(
    grades: Dict[str, Dict[str, Any]],
    path: str,
    include_ec: bool = True,
) -> None:
    """Write per-student grades as a CSV."""
    if not grades:
        print("No grades to write.")
        return

    video_items = list(ALL_VIDEO_ITEMS) if include_ec else list(VIDEO_RUBRIC_ITEMS)
    llm_items = list(ALL_LLM_ITEMS) if include_ec else list(LLM_RUBRIC_ITEMS)

    fieldnames = ["student"]
    for item in video_items:
        fieldnames.append(f"video_{item}")
    fieldnames.append("video_total")
    for item in llm_items:
        fieldnames.append(f"llm_{item}")
    fieldnames.append("llm_total")
    max_pts = next(iter(grades.values()))["grand_max"]
    fieldnames.append(f"grand_total (max {max_pts})")

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for student in sorted(grades):
            row = {"student": student}
            g = grades[student]
            for item in video_items:
                row[f"video_{item}"] = g.get(f"video_{item}", "NO_DATA")
            row["video_total"] = g.get("video_total", 0)
            for item in llm_items:
                row[f"llm_{item}"] = g.get(f"llm_{item}", "NO_DATA")
            row["llm_total"] = g.get("llm_total", 0)
            row[f"grand_total (max {max_pts})"] = g.get("grand_total", 0)
            writer.writerow(row)

    print(f"Grades CSV written to {path}")


def write_reports(
    grades: Dict[str, Dict[str, Any]],
    reports_dir: str,
    include_ec: bool = True,
) -> None:
    """Write per-student text reports."""
    os.makedirs(reports_dir, exist_ok=True)

    video_items = list(ALL_VIDEO_ITEMS) if include_ec else list(VIDEO_RUBRIC_ITEMS)
    llm_items = list(ALL_LLM_ITEMS) if include_ec else list(LLM_RUBRIC_ITEMS)

    for student in sorted(grades):
        g = grades[student]
        lines: List[str] = []
        lines.append(f"Lab 3 Grade Report: {student}")
        lines.append("=" * 60)

        lines.append("")
        lines.append(f"Grand Total: {g['grand_total']} / {g['grand_max']}")
        lines.append(f"  Video: {g['video_total']} / {g['video_max']}")
        lines.append(f"  Code Review: {g['llm_total']} / {g['llm_max']}")

        lines.append("")
        lines.append("Video Analysis")
        lines.append("-" * 40)
        for item in video_items:
            verdict = g.get(f"video_{item}", "NO_DATA")
            pts = g.get(f"video_{item}_pts", 0)
            max_pts = ALL_VIDEO_POINTS.get(item, 1)
            desc = ALL_VIDEO_DESCRIPTIONS.get(item, item)
            ec_tag = " [EC]" if item in EC_VIDEO_RUBRIC_ITEMS else ""
            lines.append(f"  [{verdict:7s}] {pts}/{max_pts}  {desc}{ec_tag}")

        lines.append("")
        lines.append("Code Review (LLM)")
        lines.append("-" * 40)
        for item in llm_items:
            verdict = g.get(f"llm_{item}", "NO_DATA")
            pts = g.get(f"llm_{item}_pts", 0)
            max_pts = ALL_LLM_POINTS.get(item, 1)
            desc = ALL_LLM_DESCRIPTIONS.get(item, item)
            ec_tag = " [EC]" if item in EC_LLM_RUBRIC_ITEMS else ""
            lines.append(f"  [{verdict:7s}] {pts}/{max_pts}  {desc}{ec_tag}")

        lines.append("")
        report_path = os.path.join(reports_dir, f"{student}.txt")
        with open(report_path, "w") as f:
            f.write("\n".join(lines) + "\n")

    print(f"Reports written to {reports_dir}/ ({len(grades)} students)")


# =====================================================================
# HTML report generation
# =====================================================================

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Lab 3 — {student}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Helvetica, Arial, sans-serif;
    max-width: 720px; margin: 30px auto; padding: 0 20px;
    color: #1a1a1a; background: #fafafa;
  }}
  h1 {{ margin-bottom: 4px; }}
  .subtitle {{ color: #555; margin-top: 0; }}
  .banner {{
    text-align: center; padding: 18px; border-radius: 10px;
    margin: 20px 0; font-size: 1.6em; font-weight: 600;
  }}
  .banner-good {{ background: #e8f5e9; color: #2e7d32; }}
  .banner-mid  {{ background: #fff8e1; color: #f57f17; }}
  .banner-low  {{ background: #ffebee; color: #c62828; }}
  table {{
    width: 100%; border-collapse: collapse; margin-bottom: 24px;
  }}
  th {{
    text-align: left; padding: 8px 10px; background: #e0e0e0;
    font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.5px;
  }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #e8e8e8; }}
  tr.pass td:first-child {{ color: #2e7d32; font-weight: 600; }}
  tr.fail td:first-child {{ color: #c62828; font-weight: 600; }}
  tr.nodata td:first-child {{ color: #9e9e9e; }}
  tr.ec {{ background: #f3e5f5; }}
  .section-header {{
    margin-top: 28px; margin-bottom: 8px; padding-bottom: 4px;
    border-bottom: 2px solid #1565c0; color: #1565c0;
  }}
  .summary {{ color: #555; margin-bottom: 4px; }}
  .footer {{
    margin-top: 30px; padding-top: 12px; border-top: 1px solid #ddd;
    font-size: 0.8em; color: #999;
  }}
</style>
</head>
<body>
<h1>Lab 3 Grade Report</h1>
<p class="subtitle">{student}</p>

{banner}

{video_section}

{ec_section}

<div class="footer">
  Generated by DALI automated grading. Video-based analysis only.
</div>
</body>
</html>
"""


def _verdict_row(verdict: str, desc: str, is_ec: bool = False) -> str:
    """Build one HTML table row."""
    css = "pass" if verdict == "PASS" else ("fail" if verdict == "FAIL" else "nodata")
    if is_ec:
        css += " ec"
    return (f'<tr class="{css}">'
            f"<td>{verdict}</td>"
            f"<td>{desc}</td>"
            f"</tr>\n")


def compute_canvas_score(
    grades_entry: Dict[str, Any],
    base_total: int = 100,
    ec_total: int = 10,
) -> int:
    """Compute the Canvas score (video-only).

    Base video items scale to ``base_total`` (default 100).
    EC video items scale to ``ec_total`` (default 10) on top.
    Full base marks = 100, full EC = 110.
    """
    base_earned = 0
    ec_earned = 0
    base_max = sum(ALL_VIDEO_POINTS.get(k, 1) for k in VIDEO_RUBRIC_ITEMS)
    ec_max = sum(ALL_VIDEO_POINTS.get(k, 1) for k in EC_VIDEO_RUBRIC_ITEMS)

    for item in VIDEO_RUBRIC_ITEMS:
        if grades_entry.get(f"video_{item}") == "PASS":
            base_earned += ALL_VIDEO_POINTS.get(item, 1)

    for item in EC_VIDEO_RUBRIC_ITEMS:
        if grades_entry.get(f"video_{item}") == "PASS":
            ec_earned += ALL_VIDEO_POINTS.get(item, 1)

    scaled_base = round(base_earned / base_max * base_total)
    scaled_ec = round(ec_earned / ec_max * ec_total) if ec_max else 0

    return scaled_base + scaled_ec


def write_html_reports(
    grades: Dict[str, Dict[str, Any]],
    reports_dir: str,
) -> Dict[str, str]:
    """Generate per-student HTML grade pages (video-only).

    Returns dict mapping student name -> path to HTML file.
    """
    os.makedirs(reports_dir, exist_ok=True)
    paths: Dict[str, str] = {}

    for student in sorted(grades):
        g = grades[student]
        score = compute_canvas_score(g)

        # Banner
        if score >= 90:
            banner_css = "banner-good"
        elif score >= 60:
            banner_css = "banner-mid"
        else:
            banner_css = "banner-low"

        base_pass = sum(
            1 for k in VIDEO_RUBRIC_ITEMS
            if g.get(f"video_{k}") == "PASS"
        )
        ec_pass = sum(
            1 for k in EC_VIDEO_RUBRIC_ITEMS
            if g.get(f"video_{k}") == "PASS"
        )

        banner = (f'<div class="banner {banner_css}">'
                  f"Score: {score} / 100"
                  f"</div>")

        # Base video items table
        rows = []
        for item in VIDEO_RUBRIC_ITEMS:
            verdict = g.get(f"video_{item}", "NO_DATA")
            desc = ALL_VIDEO_DESCRIPTIONS.get(item, item)
            rows.append(_verdict_row(verdict, desc))

        video_section = (
            f'<h2 class="section-header">Video Analysis — {score} / 100</h2>\n'
            f'<p class="summary">{base_pass} / {len(VIDEO_RUBRIC_ITEMS)}'
            f" items passed</p>\n"
            f"<table>\n"
            f"<tr><th>Result</th><th>Rubric Item</th></tr>\n"
            f"{''.join(rows)}"
            f"</table>"
        )

        # EC section
        if ec_pass > 0:
            ec_rows = []
            for item in EC_VIDEO_RUBRIC_ITEMS:
                verdict = g.get(f"video_{item}", "NO_DATA")
                desc = ALL_VIDEO_DESCRIPTIONS.get(item, item)
                ec_rows.append(_verdict_row(verdict, desc, is_ec=True))
            ec_bonus = round(ec_pass / len(EC_VIDEO_RUBRIC_ITEMS) * 10)
            ec_section = (
                f'<h2 class="section-header">Extra Credit — +{ec_bonus} bonus</h2>\n'
                f'<p class="summary">{ec_pass} / '
                f"{len(EC_VIDEO_RUBRIC_ITEMS)} EC items passed</p>\n"
                f"<table>\n"
                f"<tr><th>Result</th>"
                f"<th>Rubric Item</th></tr>\n"
                f"{''.join(ec_rows)}"
                f"</table>"
            )
        else:
            ec_section = ""

        html = _HTML_TEMPLATE.format(
            student=student,
            banner=banner,
            video_section=video_section,
            ec_section=ec_section,
        )

        path = os.path.join(reports_dir, f"{student}.html")
        with open(path, "w") as f:
            f.write(html)
        paths[student] = path

    print(f"HTML reports written to {reports_dir}/ ({len(paths)} students)")
    return paths


# =====================================================================
# CLI
# =====================================================================


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m grading.lab3.score_results",
        description="Score Lab 3 video + LLM results and generate grades.",
    )
    parser.add_argument(
        "--export-rubric", metavar="FILE",
        help="Export the default rubric YAML for editing and exit",
    )
    parser.add_argument(
        "--video-results", metavar="FILE",
        help="Video results CSV from --analyze-videos")
    parser.add_argument("--llm-results", metavar="FILE")
    parser.add_argument("--rubric", metavar="FILE", help="Rubric YAML")
    parser.add_argument("--grades-csv", metavar="FILE")
    parser.add_argument("--reports-dir", metavar="DIR")
    parser.add_argument(
        "--html-reports-dir", metavar="DIR",
        help="Generate per-student HTML grade pages in DIR",
    )
    parser.add_argument(
        "--no-ec", action="store_true",
        help="Exclude extra-credit items from scoring",
    )

    args = parser.parse_args(argv)

    if args.export_rubric:
        export_rubric(args.export_rubric)
        return 0

    if not args.video_results and not args.llm_results:
        parser.error(
            "supply --video-results and/or --llm-results, "
            "or use --export-rubric"
        )

    if args.rubric:
        load_rubric(args.rubric)

    video_results = None
    if args.video_results:
        video_results = load_video_results_csv(args.video_results)

    llm_results = None
    if args.llm_results:
        with open(args.llm_results) as f:
            llm_results = json.load(f)

    include_ec = not args.no_ec
    grades = generate_grades(video_results, llm_results, include_ec=include_ec)

    if not grades:
        print("No students found in results.")
        return 1

    print(f"Scored {len(grades)} students")

    if args.grades_csv:
        write_grades_csv(grades, args.grades_csv, include_ec=include_ec)

    if args.reports_dir:
        write_reports(grades, args.reports_dir, include_ec=include_ec)

    if args.html_reports_dir:
        write_html_reports(grades, args.html_reports_dir)

    # Summary
    totals = [g["grand_total"] for g in grades.values()]
    maxes = [g["grand_max"] for g in grades.values()]
    max_pts = maxes[0] if maxes else 0
    avg = sum(totals) / len(totals) if totals else 0
    print(f"Average: {avg:.1f} / {max_pts}")
    print(f"Range: {min(totals)} - {max(totals)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
