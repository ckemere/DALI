#!/usr/bin/env python3
"""
DRC Report Generator for DALI

Converts KiCad DRC JSON output into a clean, readable HTML report.
Filters out warnings — only errors are shown.

Usage:
    python drc_report_generator.py input.json output.html [--title "Report Title"]

Also usable as a library:
    from drc_report_generator import generate_html_report
    html = generate_html_report(json_path, title="Minimum Specs")
"""

import json
import os
import sys
import argparse
from datetime import datetime


def load_drc_json(json_path):
    """Load and parse a KiCad DRC JSON report."""
    with open(json_path, "r") as f:
        return json.load(f)


def filter_errors(data):
    """
    Extract only error-severity items from the DRC report.

    KiCad 9 splits results across multiple top-level arrays:
      - violations: design rule violations (clearance, track width, etc.)
      - unconnected_items: missing connections in the netlist
      - schematic_parity: mismatches between schematic and PCB

    Returns:
        list of violation dicts, each with at minimum:
            - type (str)
            - severity (str)
            - description (str)
            - items (list of affected items with positions)
    """
    errors = []
    for key in ("violations", "unconnected_items", "schematic_parity"):
        for v in data.get(key, []):
            if v.get("severity") == "error":
                errors.append(v)
    return errors


def count_warnings(data):
    """Count warnings across all DRC sections."""
    count = 0
    for key in ("violations", "unconnected_items", "schematic_parity"):
        for v in data.get(key, []):
            if v.get("severity") == "warning":
                count += 1
    return count


def format_position(pos):
    """Format a position dict into a readable string (mm)."""
    if not pos:
        return ""
    x = pos.get("x", 0)
    y = pos.get("y", 0)
    return f"({x:.2f}, {y:.2f}) mm"


def generate_html_report(json_path, title="DRC Report"):
    """
    Generate a self-contained HTML report from a KiCad DRC JSON file.

    Args:
        json_path: Path to the DRC JSON output.
        title:     Human-readable title for the report.

    Returns:
        The HTML content as a string.
    """
    data = load_drc_json(json_path)
    errors = filter_errors(data)
    warning_count = count_warnings(data)

    # Extract metadata
    source = data.get("source", "")
    coordinate_units = data.get("coordinate_units", "mm")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    # Determine pass/fail
    passed = len(errors) == 0
    status_class = "passed" if passed else "failed"
    status_text = "PASSED" if passed else f"FAILED — {len(errors)} error{'s' if len(errors) != 1 else ''}"
    status_icon = "✓" if passed else "✗"

    # Build violation rows
    violation_rows = ""
    for i, err in enumerate(errors, 1):
        desc = err.get("description", "Unknown violation")
        err_type = err.get("type", "unknown")

        # Build items detail
        items_html = ""
        for item in err.get("items", []):
            item_desc = item.get("description", "")
            pos = item.get("pos", {})
            pos_str = format_position(pos) if pos else ""
            if item_desc or pos_str:
                items_html += f'<div class="item">'
                if item_desc:
                    items_html += f'<span class="item-desc">{_escape(item_desc)}</span>'
                if pos_str:
                    items_html += f' <span class="item-pos">{pos_str}</span>'
                items_html += "</div>"

        violation_rows += f"""
        <tr>
            <td class="err-num">{i}</td>
            <td class="err-type">{_escape(err_type)}</td>
            <td>
                <div class="err-desc">{_escape(desc)}</div>
                {items_html}
            </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DRC: {_escape(title)}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f5f5f5;
            padding: 20px;
            color: #333;
        }}

        .container {{
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            overflow: hidden;
        }}

        .header {{
            padding: 24px 30px;
            border-bottom: 2px solid #e0e0e0;
        }}

        .header h1 {{
            font-size: 22px;
            margin-bottom: 8px;
        }}

        .header .meta {{
            font-size: 13px;
            color: #888;
        }}

        .status-banner {{
            padding: 16px 30px;
            font-size: 18px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .status-banner.passed {{
            background-color: #e8f5e9;
            color: #2e7d32;
            border-bottom: 2px solid #4caf50;
        }}

        .status-banner.failed {{
            background-color: #fce4ec;
            color: #c62828;
            border-bottom: 2px solid #e53935;
        }}

        .status-icon {{
            font-size: 24px;
        }}

        .summary {{
            padding: 16px 30px;
            font-size: 14px;
            color: #666;
            border-bottom: 1px solid #eee;
        }}

        .violations {{
            padding: 20px 30px;
        }}

        .violations h2 {{
            font-size: 16px;
            margin-bottom: 12px;
            color: #333;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        th {{
            background-color: #f5f5f5;
            text-align: left;
            padding: 10px 12px;
            font-size: 13px;
            font-weight: 600;
            color: #555;
            border-bottom: 2px solid #ddd;
        }}

        td {{
            padding: 10px 12px;
            border-bottom: 1px solid #eee;
            font-size: 14px;
            vertical-align: top;
        }}

        .err-num {{
            width: 40px;
            text-align: center;
            color: #999;
            font-weight: 600;
        }}

        .err-type {{
            width: 160px;
            font-family: 'Courier New', monospace;
            font-size: 12px;
            color: #666;
            word-break: break-all;
        }}

        .err-desc {{
            font-weight: 500;
            margin-bottom: 4px;
        }}

        .item {{
            font-size: 12px;
            color: #666;
            padding: 2px 0 2px 12px;
            border-left: 2px solid #ddd;
            margin-top: 4px;
        }}

        .item-pos {{
            font-family: 'Courier New', monospace;
            font-size: 11px;
            color: #999;
        }}

        .no-errors {{
            text-align: center;
            padding: 40px 20px;
            color: #4caf50;
            font-size: 16px;
        }}

        .footer {{
            padding: 12px 30px;
            font-size: 11px;
            color: #aaa;
            border-top: 1px solid #eee;
            text-align: right;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>DRC Report: {_escape(title)}</h1>
            <div class="meta">
                Generated {timestamp}
                {f' &bull; Source: {_escape(os.path.basename(source))}' if source else ''}
            </div>
        </div>

        <div class="status-banner {status_class}">
            <span class="status-icon">{status_icon}</span>
            <span>{status_text}</span>
        </div>

        <div class="summary">
            {len(errors)} error{'s' if len(errors) != 1 else ''}
            {f' &bull; {warning_count} warning{"s" if warning_count != 1 else ""} (hidden)' if warning_count else ''}
        </div>

        <div class="violations">
            {f'<h2>Errors</h2>' if errors else ''}
            {"" if not errors else f'''
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Rule</th>
                        <th>Description</th>
                    </tr>
                </thead>
                <tbody>
                    {violation_rows}
                </tbody>
            </table>
            '''}
            {"<div class='no-errors'>No design rule errors found.</div>" if not errors else ""}
        </div>

        <div class="footer">
            DALI DRC Report &bull; Warnings excluded
        </div>
    </div>
</body>
</html>"""

    return html


def _escape(text):
    """Basic HTML escaping."""
    if not isinstance(text, str):
        text = str(text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def main():
    parser = argparse.ArgumentParser(description="Convert KiCad DRC JSON to HTML report")
    parser.add_argument("input", help="Path to DRC JSON file")
    parser.add_argument("output", help="Path for output HTML file")
    parser.add_argument("--title", default="DRC Report", help="Report title")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    html = generate_html_report(args.input, title=args.title)

    with open(args.output, "w") as f:
        f.write(html)

    # Print a quick summary to stdout (captured by Make)
    data = load_drc_json(args.input)
    errors = filter_errors(data)
    status = "PASSED" if not errors else f"FAILED ({len(errors)} errors)"
    print(f"  DRC [{args.title}]: {status}")


if __name__ == "__main__":
    main()
