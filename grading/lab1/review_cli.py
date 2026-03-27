#!/usr/bin/env python3
"""
CLI for Gemini-based code review of Lab 1 submissions.

Modes:
  Single submission (extracted directory):
    python -m grading.lab1.review_cli --dir ./student_submission/

  Single submission (zip file):
    python -m grading.lab1.review_cli --zip ./student.zip

  Batch (directory of zips):
    python -m grading.lab1.review_cli --batch ./submissions/

Environment:
  GEMINI_API_KEY   — required (Google AI Studio or Vertex API key)
"""

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime

from assess.build import extract_submission, student_name_from_zip
from assess.code_review import (
    RUBRIC_ITEMS,
    review_submission,
    format_results,
    DEFAULT_MODEL,
)


def _review_directory(submission_dir, *, api_key, model, verbose):
    """Review an already-extracted submission directory."""
    results = review_submission(
        submission_dir, api_key=api_key, model=model, verbose=verbose,
    )
    return results


def _review_zip(zip_path, *, api_key, model, verbose, keep_build=False):
    """Extract a zip to a temp dir, review, clean up."""
    build_dir = tempfile.mkdtemp(prefix="review_")
    try:
        extract_submission(zip_path, build_dir)
        results = _review_directory(
            build_dir, api_key=api_key, model=model, verbose=verbose,
        )
        return results
    finally:
        if not keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)


def _write_csv(rows, output_path):
    """Write batch results to CSV."""
    fieldnames = ["student", "zip_file"]
    for item_id in RUBRIC_ITEMS:
        fieldnames.append(f"{item_id}_verdict")
        fieldnames.append(f"{item_id}_reason")
        fieldnames.append(f"{item_id}_evidence")
    fieldnames.append("error")

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Gemini-based code review for Lab 1 rubric items",
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--dir",
        help="Path to an already-extracted submission directory",
    )
    source.add_argument(
        "--zip",
        help="Path to a single student .zip submission",
    )
    source.add_argument(
        "--batch",
        help="Directory of student .zip files for batch review",
    )

    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Gemini model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--api-key",
        help="Gemini API key (default: GEMINI_API_KEY env var)",
    )
    parser.add_argument(
        "--results-csv",
        default=f"lab1_code_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        help="Output CSV file for batch results",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output raw JSON instead of formatted text (single modes)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print the full prompt and raw Gemini response",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color in output",
    )

    args = parser.parse_args()
    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")

    if not api_key:
        print("Error: set GEMINI_API_KEY or pass --api-key", file=sys.stderr)
        sys.exit(1)

    # ── Single directory mode ─────────────────────────────────────
    if args.dir:
        if not os.path.isdir(args.dir):
            print(f"Error: {args.dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        print(f"Reviewing submission in {args.dir} ...\n")
        results = _review_directory(
            args.dir, api_key=api_key, model=args.model,
            verbose=args.verbose,
        )
        if args.json_output:
            print(json.dumps(results, indent=2))
        else:
            print(format_results(results, use_color=not args.no_color))
        sys.exit(0)

    # ── Single zip mode ───────────────────────────────────────────
    if args.zip:
        if not os.path.isfile(args.zip):
            print(f"Error: {args.zip} not found", file=sys.stderr)
            sys.exit(1)
        student = student_name_from_zip(os.path.basename(args.zip))
        print(f"Reviewing {student} ({args.zip}) ...\n")
        try:
            results = _review_zip(
                args.zip, api_key=api_key, model=args.model,
                verbose=args.verbose,
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if args.json_output:
            print(json.dumps(results, indent=2))
        else:
            print(format_results(results, use_color=not args.no_color))
        sys.exit(0)

    # ── Batch mode ────────────────────────────────────────────────
    if not os.path.isdir(args.batch):
        print(f"Error: {args.batch} is not a directory", file=sys.stderr)
        sys.exit(1)

    zip_files = sorted(
        f for f in os.listdir(args.batch) if f.endswith(".zip")
    )
    if not zip_files:
        print(f"No .zip files found in {args.batch}")
        sys.exit(1)

    print(f"Found {len(zip_files)} submissions.  Model: {args.model}\n")

    rows = []
    for i, zip_name in enumerate(zip_files, 1):
        student = student_name_from_zip(zip_name)
        zip_path = os.path.join(args.batch, zip_name)
        print(f"[{i}/{len(zip_files)}] {student} ", end="", flush=True)

        row = {"student": student, "zip_file": zip_name}

        try:
            results = _review_zip(
                zip_path, api_key=api_key, model=args.model,
                verbose=args.verbose,
            )
            passes = 0
            fails = 0
            for item_id in RUBRIC_ITEMS:
                entry = results.get(item_id, {})
                if isinstance(entry, dict):
                    row[f"{item_id}_verdict"] = entry.get("verdict", "MISSING")
                    row[f"{item_id}_reason"] = entry.get("reason", "")
                    row[f"{item_id}_evidence"] = entry.get("evidence", "")
                    if entry.get("verdict") == "PASS":
                        passes += 1
                    elif entry.get("verdict") == "FAIL":
                        fails += 1
                else:
                    row[f"{item_id}_verdict"] = "UNCLEAR"
                    row[f"{item_id}_reason"] = str(entry)
                    row[f"{item_id}_evidence"] = ""

            print(f"  PASS={passes}  FAIL={fails}  "
                  f"OTHER={len(RUBRIC_ITEMS) - passes - fails}")

        except Exception as e:
            row["error"] = str(e)
            print(f"  ERROR: {e}")

        rows.append(row)

    # Write CSV
    _write_csv(rows, args.results_csv)
    print(f"\nResults written to {args.results_csv}")

    ok = sum(1 for r in rows if "error" not in r)
    print(f"Summary: {ok}/{len(rows)} reviewed successfully")


if __name__ == "__main__":
    main()
