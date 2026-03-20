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
  python grade_pcbs.py /path/to/canvas/zips/ /path/to/template_dir/ -o grades.csv

  template_dir should contain the lab's .kicad_dru files and lab.yaml
  (e.g. template_files/lab4/).

Requirements:
  - kicad-cli on PATH (for DRC)
  - Python 3.10+
"""

import argparse
import base64
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import Optional


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
# KiCad S-expression parser (minimal, no pcbnew dependency)
# ---------------------------------------------------------------------------

def _tokenize(text: str):
    """Yield tokens from KiCad S-expression text."""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            yield "("
            i += 1
        elif c == ")":
            yield ")"
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                j += 1
            yield text[i + 1 : j]
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n()\"":
                j += 1
            yield text[i:j]
            i = j


def _parse_sexpr(tokens) -> list:
    """Parse a single S-expression from a token iterator. Returns nested lists."""
    result = []
    for tok in tokens:
        if tok == "(":
            result.append(_parse_sexpr(tokens))
        elif tok == ")":
            return result
        else:
            # try numeric conversion
            try:
                result.append(float(tok) if "." in tok else int(tok))
            except ValueError:
                result.append(tok)
    return result


def parse_kicad_pcb(pcb_path: Path) -> list:
    """Parse a .kicad_pcb file into nested lists."""
    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    tokens = _tokenize(text)
    # The file is one big S-expression wrapped in parens
    result = []
    for tok in tokens:
        if tok == "(":
            result.append(_parse_sexpr(tokens))
        else:
            result.append(tok)
    return result[0] if len(result) == 1 else result


def _find_nodes(tree, tag):
    """Recursively find all sub-lists whose first element == tag."""
    results = []
    if isinstance(tree, list) and len(tree) > 0:
        if tree[0] == tag:
            results.append(tree)
        for child in tree:
            results.extend(_find_nodes(child, tag))
    return results


def _get_attr(node, key, default=None):
    """In a node like ['segment', ['start', 1, 2], ...], find ['start', ...] and return rest."""
    for child in node:
        if isinstance(child, list) and len(child) > 1 and child[0] == key:
            return child[1] if len(child) == 2 else child[1:]
    return default


# ---------------------------------------------------------------------------
# Board dimensions from Edge.Cuts
# ---------------------------------------------------------------------------

def compute_board_bbox(tree) -> tuple[float, float, float]:
    """
    Compute the bounding box of all Edge.Cuts geometry.

    Returns (width_mm, height_mm, area_mm2).
    Area is approximated as width * height for the bounding box.
    """
    min_x = float("inf")
    max_x = float("-inf")
    min_y = float("inf")
    max_y = float("-inf")

    def update(x, y):
        nonlocal min_x, max_x, min_y, max_y
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)

    def is_edge_cuts(node):
        layer = _get_attr(node, "layer")
        if layer is None:
            return False
        if isinstance(layer, list):
            layer = layer[0]
        return layer in ("Edge.Cuts",)

    # Top-level graphical items: gr_line, gr_arc, gr_circle, gr_rect, gr_poly
    for node in _find_nodes(tree, "gr_line"):
        if not is_edge_cuts(node):
            continue
        start = _get_attr(node, "start")
        end = _get_attr(node, "end")
        if start and end:
            update(start[0], start[1])
            update(end[0], end[1])

    for node in _find_nodes(tree, "gr_rect"):
        if not is_edge_cuts(node):
            continue
        start = _get_attr(node, "start")
        end = _get_attr(node, "end")
        if start and end:
            update(start[0], start[1])
            update(end[0], end[1])

    for node in _find_nodes(tree, "gr_arc"):
        if not is_edge_cuts(node):
            continue
        # For arcs, the bounding box of start/mid/end is a conservative
        # approximation; a true arc bbox needs trig but this covers most cases.
        for key in ("start", "mid", "end"):
            pt = _get_attr(node, key)
            if pt:
                update(pt[0], pt[1])

    for node in _find_nodes(tree, "gr_circle"):
        if not is_edge_cuts(node):
            continue
        center = _get_attr(node, "center")
        end = _get_attr(node, "end")
        if center and end:
            r = math.hypot(end[0] - center[0], end[1] - center[1])
            update(center[0] - r, center[1] - r)
            update(center[0] + r, center[1] + r)

    for node in _find_nodes(tree, "gr_poly"):
        if not is_edge_cuts(node):
            continue
        pts_node = _get_attr(node, "pts")
        if pts_node:
            # pts_node is like ['pts', ['xy', x, y], ['xy', x, y], ...]
            # but _get_attr returns child[1:], so we get the list after 'pts'
            for child in node:
                if isinstance(child, list) and child[0] == "pts":
                    for xy in child[1:]:
                        if isinstance(xy, list) and xy[0] == "xy":
                            update(xy[1], xy[2])

    # Also check footprint drawings on Edge.Cuts (some students draw outlines
    # inside footprints rather than as top-level graphics)
    for fp in _find_nodes(tree, "footprint"):
        for item_type in ("fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly"):
            for node in _find_nodes(fp, item_type):
                if not is_edge_cuts(node):
                    continue
                for key in ("start", "end", "mid", "center"):
                    pt = _get_attr(node, key)
                    if pt and isinstance(pt, list) and len(pt) >= 2:
                        update(pt[0], pt[1])

    if min_x == float("inf"):
        return (0.0, 0.0, 0.0)

    w = max_x - min_x
    h = max_y - min_y
    return (round(w, 3), round(h, 3), round(w * h, 3))


# ---------------------------------------------------------------------------
# Copper-layer text extraction
# ---------------------------------------------------------------------------

COPPER_LAYERS = {"F.Cu", "B.Cu"}


def extract_copper_texts(tree) -> list[dict]:
    """
    Extract all text items placed on copper layers.

    Returns list of dicts with keys: text, layer, source.
    Source is 'graphic' for top-level gr_text, or 'footprint' for fp_text
    inside a footprint.
    """
    results = []

    # Top-level gr_text items
    for node in _find_nodes(tree, "gr_text"):
        layer = _get_attr(node, "layer")
        if isinstance(layer, list):
            layer = layer[0]
        if layer not in COPPER_LAYERS:
            continue
        # The text content is the second element: (gr_text "hello" (at ...) ...)
        if len(node) >= 2 and isinstance(node[1], str):
            text = node[1]
            if text and text != "${REFERENCE}":
                results.append({"text": text, "layer": layer, "source": "graphic"})

    # fp_text inside footprints — but only user-placed text on copper,
    # not standard reference/value designators
    for fp in _find_nodes(tree, "footprint"):
        for node in _find_nodes(fp, "fp_text"):
            # fp_text has form: (fp_text <type> "text" (at ...) (layer ...) ...)
            # where <type> is "reference", "value", or "user"
            if len(node) < 3:
                continue
            text_type = node[1]
            text_val = node[2] if len(node) > 2 else ""

            layer = _get_attr(node, "layer")
            if isinstance(layer, list):
                layer = layer[0]
            if layer not in COPPER_LAYERS:
                continue

            # Skip template placeholders
            if isinstance(text_val, str) and text_val.startswith("${"):
                continue

            if isinstance(text_val, str) and text_val.strip():
                results.append({
                    "text": text_val,
                    "layer": layer,
                    "source": f"footprint ({text_type})",
                })

    # Also check for gr_text_box (KiCad 8+)
    for node in _find_nodes(tree, "gr_text_box"):
        layer = _get_attr(node, "layer")
        if isinstance(layer, list):
            layer = layer[0]
        if layer not in COPPER_LAYERS:
            continue
        if len(node) >= 2 and isinstance(node[1], str):
            text = node[1]
            if text:
                results.append({"text": text, "layer": layer, "source": "text_box"})

    return results


# ---------------------------------------------------------------------------
# DRC via kicad-cli
# ---------------------------------------------------------------------------

KICAD_CLI = os.environ.get("KICAD_CLI_PATH", "kicad-cli")


def run_drc(pcb_path: Path, dru_path: Path, output_json: Path) -> tuple[bool, int]:
    """
    Run DRC on a PCB with a specific DRU file.

    KiCad looks for a sidecar .kicad_dru file matching the PCB basename.
    We copy the DRU into place, run DRC, then clean up.

    Returns (passed: bool, error_count: int).
    """
    pcb_stem = pcb_path.stem
    sidecar = pcb_path.parent / f"{pcb_stem}.kicad_dru"

    try:
        shutil.copy2(dru_path, sidecar)

        cmd = [
            KICAD_CLI, "pcb", "drc",
            "--format", "json",
            "--output", str(output_json),
            str(pcb_path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
        # kicad-cli returns non-zero if violations found; we still parse JSON

        if not output_json.exists():
            print(f"    DRC produced no output for {pcb_path.name}")
            return (False, -1)

        with open(output_json) as f:
            data = json.load(f)

        error_count = 0
        for key in ("violations", "unconnected_items", "schematic_parity"):
            for v in data.get(key, []):
                if v.get("severity") == "error":
                    error_count += 1

        return (error_count == 0, error_count)

    except subprocess.TimeoutExpired:
        print(f"    DRC timed out for {pcb_path.name}")
        return (False, -1)
    except Exception as e:
        print(f"    DRC error for {pcb_path.name}: {e}")
        return (False, -1)
    finally:
        if sidecar.exists():
            sidecar.unlink()


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
        f'alt="{html_escape(alt)}" style="max-width:300px; max-height:300px;">'
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
