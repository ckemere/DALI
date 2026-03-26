#!/usr/bin/env python3
"""
grade_pcbs.py — Autograde a batch of KiCad PCB submissions from Canvas.

For each student submission zip, this script:
  1. Runs KiCad DRC against weak and strong DRU rule files
  2. Computes the board bounding-box dimensions and area from Edge.Cuts geometry
  3. Extracts text items placed on copper layers (F.Cu / B.Cu) as a first-pass
     check for student initials

Outputs a CSV with one row per student.

Usage:
  python -m grading.lab4.grade_pcbs /path/to/canvas/zips/ /path/to/template_dir/ -o grades.csv

  template_dir should contain the lab's .kicad_dru files and lab.yaml
  (e.g. template_files/lab4/).

Requirements:
  - kicad-cli on PATH (for DRC)
  - Python 3.10+
"""

import argparse
import base64
import csv
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from html import escape as html_escape
from pathlib import Path
from typing import Optional

from assess.pcb import (
    parse_kicad_pcb,
    compute_board_bbox,
    extract_copper_texts,
    run_drc,
    KICAD_CLI,
)


# ---------------------------------------------------------------------------
# Canvas zip filename parsing (shared pattern with panelize_pcbs.py)
# ---------------------------------------------------------------------------

# Canvas zip filename patterns:
#   studentname_canvasid_submissionid_Assignment_Name_netid-version.zip
#   studentname_canvasid_submissionid_Assignment_Name_netid.zip  (v0)
#   studentname_LATE_canvasid_submissionid_Assignment_Name_netid-version.zip
ZIP_PATTERN = re.compile(
    r"^(?P<student>.+?)_(?:LATE_)?(?P<canvasid>\d+)_(?P<subid>\d+)"
    r"_(?P<assignment>.+)_(?P<netid>[a-zA-Z0-9]+)(?:-(?P<version>\d+))?\.zip$"
)


@dataclass
class Submission:
    student_name: str
    net_id: str
    version: int
    zip_path: Path
    late: bool = False


def parse_submissions(zip_dir: Path) -> list[Submission]:
    """Parse Canvas zip filenames, keep latest version per student."""
    by_student: dict[str, Submission] = {}
    n_zips = 0

    for f in sorted(zip_dir.iterdir()):
        if not f.name.endswith(".zip"):
            continue
        n_zips += 1
        m = ZIP_PATTERN.match(f.name)
        if not m:
            print(f"  WARNING: Skipping unrecognized zip: {f.name}")
            continue

        student = m.group("student")
        net_id = m.group("netid")
        version = int(m.group("version")) if m.group("version") else 0
        late = "LATE" in f.name.split("_")[:3]

        if student not in by_student or version > by_student[student].version:
            by_student[student] = Submission(
                student_name=student,
                net_id=net_id,
                version=version,
                zip_path=f,
                late=late,
            )

    subs = sorted(by_student.values(), key=lambda s: s.student_name)
    print(f"Found {len(subs)} unique students (from {n_zips} zips)")
    return subs


# ---------------------------------------------------------------------------
# Zip extraction
# ---------------------------------------------------------------------------

def extract_pcb(sub: Submission, work_dir: Path) -> Optional[Path]:
    """Extract a submission zip; return path to .kicad_pcb or None."""
    student_dir = work_dir / sub.net_id
    student_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(sub.zip_path, "r") as zf:
            zf.extractall(student_dir)
    except zipfile.BadZipFile:
        print(f"  WARNING: Bad zip for {sub.net_id}: {sub.zip_path.name}")
        return None

    pcb_files = list(student_dir.rglob("*.kicad_pcb"))
    if not pcb_files:
        print(f"  WARNING: No .kicad_pcb in {sub.zip_path.name}")
        return None

    # Prefer files not inside _pcb_results (those are DALI artifacts)
    user_pcbs = [p for p in pcb_files if "_pcb_results" not in str(p)]
    return user_pcbs[0] if user_pcbs else pcb_files[0]


# ---------------------------------------------------------------------------
# Main grading pipeline
# ---------------------------------------------------------------------------

@dataclass
class GradeResult:
    student_name: str
    net_id: str
    late: bool
    weak_drc_pass: Optional[bool] = None
    weak_drc_errors: int = 0
    strong_drc_pass: Optional[bool] = None
    strong_drc_errors: int = 0
    width_mm: float = 0.0
    height_mm: float = 0.0
    area_mm2: float = 0.0
    copper_texts: str = ""
    preview_top: Optional[Path] = None
    preview_bottom: Optional[Path] = None
    error: str = ""


def grade_one(
    sub: Submission,
    work_dir: Path,
    dru_files: list[dict],
) -> GradeResult:
    """Grade a single student submission."""
    result = GradeResult(
        student_name=sub.student_name,
        net_id=sub.net_id,
        late=sub.late,
    )

    pcb_path = extract_pcb(sub, work_dir)
    if pcb_path is None:
        result.error = "no .kicad_pcb found"
        return result

    # --- Locate PNG previews ---
    # Canvas zips from DALI contain previews either in a _pcb_results/
    # subdirectory or flat alongside the .kicad_pcb file.
    student_dir = work_dir / sub.net_id
    preview_search_dirs = []
    # Check _pcb_results subdirectories first
    for results_dir in student_dir.rglob("_pcb_results"):
        preview_search_dirs.append(results_dir)
    # Also search anywhere in the extracted tree (flat layout from Canvas)
    preview_search_dirs.append(student_dir)

    for search_dir in preview_search_dirs:
        for png in search_dir.rglob("preview_top.png"):
            if result.preview_top is None:
                result.preview_top = png
        for png in search_dir.rglob("preview_bottom.png"):
            if result.preview_bottom is None:
                result.preview_bottom = png
        if result.preview_top and result.preview_bottom:
            break

    # --- Parse PCB for dimensions and text ---
    try:
        tree = parse_kicad_pcb(pcb_path)
    except Exception as e:
        result.error = f"parse error: {e}"
        return result

    w, h, area = compute_board_bbox(tree)
    result.width_mm = w
    result.height_mm = h
    result.area_mm2 = area

    texts = extract_copper_texts(tree)
    if texts:
        # Format: "text (layer)" separated by "; "
        parts = [f'{t["text"]} ({t["layer"]})' for t in texts]
        result.copper_texts = "; ".join(parts)

    # --- Run DRC checks ---
    dru_map = {d["label"]: d for d in dru_files}

    for label, key_pass, key_errors in [
        ("weak", "weak_drc_pass", "weak_drc_errors"),
        ("strong", "strong_drc_pass", "strong_drc_errors"),
    ]:
        # Find the matching DRU by looking for the label keyword
        dru = None
        for d in dru_files:
            if label in d["name"].lower() or label in d["label"].lower():
                dru = d
                break
        if dru is None:
            continue

        dru_path = dru["path"]
        json_out = work_dir / sub.net_id / f"drc_{label}.json"
        passed, errors = run_drc(pcb_path, dru_path, json_out)
        setattr(result, key_pass, passed)
        setattr(result, key_errors, errors)

    return result


def load_dru_files(template_dir: Path) -> list[dict]:
    """Load DRU file info from lab.yaml in the template directory."""
    import yaml

    lab_yaml = template_dir / "lab.yaml"
    if not lab_yaml.exists():
        sys.exit(f"ERROR: No lab.yaml found in {template_dir}")

    with open(lab_yaml) as f:
        meta = yaml.safe_load(f)

    dru_files = []
    for dru in meta.get("dru_files", []):
        dru_path = template_dir / dru["name"]
        if not dru_path.exists():
            sys.exit(f"ERROR: DRU file not found: {dru_path}")
        dru_files.append({
            "name": dru["name"],
            "label": dru.get("label", dru["name"]),
            "path": dru_path,
        })

    if not dru_files:
        sys.exit("ERROR: No dru_files defined in lab.yaml")

    return dru_files


def _img_tag(png_path: Optional[Path], alt: str) -> str:
    """Return an <img> tag with base64-encoded PNG, or a placeholder."""
    if png_path is None or not png_path.exists():
        return "<em>no preview</em>"
    data = base64.b64encode(png_path.read_bytes()).decode("ascii")
    return (
        f'<img src="data:image/png;base64,{data}" '
        f'alt="{html_escape(alt)}" style="max-width:300px; max-height:300px; '
        f'background:#444; padding:4px; border-radius:4px;">'
    )


def write_html_report(results: list[GradeResult], html_path: Path, no_drc: bool):
    """Write an HTML report with the grading table and embedded PCB previews."""
    rows = []
    for r in results:
        late_str = "LATE" if r.late else ""
        dims = (
            f"{r.width_mm:.1f} &times; {r.height_mm:.1f} mm"
            if r.width_mm > 0 else ""
        )

        drc_cells = ""
        if not no_drc:
            def drc_cell(passed, errors):
                if passed is None:
                    return "<td></td>"
                cls = "pass" if passed else "fail"
                label = "PASS" if passed else f"FAIL ({errors})"
                return f'<td class="{cls}">{label}</td>'
            drc_cells = drc_cell(r.weak_drc_pass, r.weak_drc_errors)
            drc_cells += drc_cell(r.strong_drc_pass, r.strong_drc_errors)

        top_img = _img_tag(r.preview_top, f"{r.net_id} top")
        bot_img = _img_tag(r.preview_bottom, f"{r.net_id} bottom")

        rows.append(
            f"<tr>"
            f"<td>{html_escape(r.student_name)}</td>"
            f"<td>{html_escape(r.net_id)}</td>"
            f"<td>{late_str}</td>"
            f"{drc_cells}"
            f"<td>{dims}</td>"
            f"<td>{html_escape(r.copper_texts)}</td>"
            f"<td>{html_escape(r.error)}</td>"
            f"<td>{top_img}</td>"
            f"<td>{bot_img}</td>"
            f"</tr>"
        )

    drc_headers = ""
    if not no_drc:
        drc_headers = "<th>Weak DRC</th><th>Strong DRC</th>"

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PCB Grading Report</title>
<style>
  body {{ font-family: sans-serif; margin: 1em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; position: sticky; top: 0; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  .pass {{ color: #1a7f37; font-weight: bold; }}
  .fail {{ color: #cf222e; font-weight: bold; }}
  img {{ display: block; }}
</style>
</head>
<body>
<h1>PCB Grading Report</h1>
<p>{len(results)} students</p>
<table>
<thead>
<tr>
  <th>Student</th><th>Net ID</th><th>Late</th>
  {drc_headers}
  <th>Dimensions</th><th>Copper Text</th><th>Error</th>
  <th>Preview (Top)</th><th>Preview (Bottom)</th>
</tr>
</thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
</body>
</html>"""

    html_path.write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Autograde KiCad PCB submissions from Canvas."
    )
    parser.add_argument(
        "zip_dir", type=Path,
        help="Directory containing Canvas submission zip files",
    )
    parser.add_argument(
        "template_dir", type=Path,
        help="Lab template directory with lab.yaml and .kicad_dru files "
             "(e.g. template_files/lab4/)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("pcb_grades.csv"),
        help="Output CSV path (default: pcb_grades.csv)",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=None,
        help="Working directory for extracted files (default: temp dir)",
    )
    parser.add_argument(
        "--no-drc", action="store_true",
        help="Skip DRC checks (only compute dimensions and extract text)",
    )
    args = parser.parse_args()

    if not args.zip_dir.is_dir():
        sys.exit(f"ERROR: Not a directory: {args.zip_dir}")

    print("=" * 60)
    print("DALI PCB Autograder")
    print("=" * 60)

    # Load DRU configuration
    dru_files = load_dru_files(args.template_dir)
    print(f"\nDRU rule sets: {', '.join(d['label'] for d in dru_files)}")

    if not args.no_drc:
        if not shutil.which(KICAD_CLI):
            print(f"\nWARNING: kicad-cli not found ({KICAD_CLI}).")
            print("DRC checks will be skipped. Set KICAD_CLI_PATH or install KiCad.")
            args.no_drc = True

    # Parse submissions
    print(f"\n[1/3] Parsing submissions from {args.zip_dir}")
    submissions = parse_submissions(args.zip_dir)
    if not submissions:
        sys.exit("ERROR: No valid submissions found!")

    # Grade each submission
    use_temp = args.work_dir is None
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="dali_grade_"))
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[2/3] Grading {len(submissions)} submissions...")
    if not args.no_drc:
        print(f"  (DRC enabled — this may take a while)")

    results: list[GradeResult] = []
    for i, sub in enumerate(submissions, 1):
        print(f"\n  [{i}/{len(submissions)}] {sub.net_id} ({sub.student_name})")
        if args.no_drc:
            # Still extract and parse, just skip DRC
            r = grade_one(sub, work_dir, [])
        else:
            r = grade_one(sub, work_dir, dru_files)
        results.append(r)

        # Quick summary line
        dims = f"{r.width_mm:.1f} x {r.height_mm:.1f} mm" if r.width_mm > 0 else "no outline"
        drc_str = ""
        if not args.no_drc:
            w = "PASS" if r.weak_drc_pass else ("FAIL" if r.weak_drc_pass is False else "N/A")
            s = "PASS" if r.strong_drc_pass else ("FAIL" if r.strong_drc_pass is False else "N/A")
            drc_str = f"  DRC: weak={w}, strong={s}"
        text_str = f'  Text: "{r.copper_texts}"' if r.copper_texts else "  Text: (none)"
        print(f"    {dims}{drc_str}{text_str}")
        if r.error:
            print(f"    ERROR: {r.error}")

    # Write CSV
    print(f"\n[3/3] Writing CSV to {args.output}")
    with open(args.output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "student_name",
            "net_id",
            "late",
            "weak_drc_pass",
            "weak_drc_errors",
            "strong_drc_pass",
            "strong_drc_errors",
            "width_mm",
            "height_mm",
            "area_mm2",
            "copper_texts",
            "error",
        ])
        for r in results:
            writer.writerow([
                r.student_name,
                r.net_id,
                r.late,
                r.weak_drc_pass if r.weak_drc_pass is not None else "",
                r.weak_drc_errors if r.weak_drc_pass is not None else "",
                r.strong_drc_pass if r.strong_drc_pass is not None else "",
                r.strong_drc_errors if r.strong_drc_pass is not None else "",
                r.width_mm,
                r.height_mm,
                r.area_mm2,
                r.copper_texts,
                r.error,
            ])

    # Write HTML report
    html_path = args.output.with_suffix(".html")
    print(f"  Writing HTML report to {html_path}")
    write_html_report(results, html_path, args.no_drc)

    print(f"\nDone! {len(results)} students graded → {args.output}")

    # Summary
    if not args.no_drc:
        weak_pass = sum(1 for r in results if r.weak_drc_pass)
        strong_pass = sum(1 for r in results if r.strong_drc_pass)
        print(f"  Weak DRC pass:   {weak_pass}/{len(results)}")
        print(f"  Strong DRC pass: {strong_pass}/{len(results)}")

    has_text = sum(1 for r in results if r.copper_texts)
    has_outline = sum(1 for r in results if r.width_mm > 0)
    print(f"  Has board outline: {has_outline}/{len(results)}")
    print(f"  Has copper text:   {has_text}/{len(results)}")

    # Clean up temp dir
    if use_temp:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
