"""
PCB design assessment primitives.

Provides per-student functions for analyzing a KiCad PCB file:
  - Makefile generation for DRC + preview export
  - S-expression parser for .kicad_pcb files
  - Board bounding-box / area computation
  - Copper-layer text extraction (student initials)
  - DRC via kicad-cli

Used by both the DALI web app (compile queue, pre-submission checks)
and the grading bulk-workflow scripts.
"""

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Tool paths
# ---------------------------------------------------------------------------

KICAD_CLI = os.environ.get("KICAD_CLI_PATH", "kicad-cli")
RSVG_CONVERT = os.environ.get("RSVG_CONVERT_PATH", "rsvg-convert")
PYTHON = os.environ.get("PYTHON_PATH", "python3")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRC_REPORT_SCRIPT = os.environ.get(
    "DRC_REPORT_SCRIPT",
    os.path.join(_REPO_ROOT, "drc_report_generator.py"),
)

PNG_DPI = int(os.environ.get("PCB_PREVIEW_DPI", "300"))

PREVIEW_LAYERS = {
    "top": "F.Cu,Edge.Cuts,F.SilkS",
    "bottom": "B.Cu,Edge.Cuts,B.SilkS",
}


# ---------------------------------------------------------------------------
# Toolchain verification
# ---------------------------------------------------------------------------

def verify_pcb_toolchain():
    """
    Verify that KiCad CLI and rsvg-convert are available.

    Returns:
        tuple: (success: bool, message: str)
    """
    missing = []
    if not shutil.which(KICAD_CLI):
        missing.append(f"kicad-cli not found (looked for: {KICAD_CLI})")
    if not shutil.which(RSVG_CONVERT):
        missing.append(f"rsvg-convert not found (looked for: {RSVG_CONVERT})")
    if not os.path.isfile(DRC_REPORT_SCRIPT):
        missing.append(f"DRC report script not found at: {DRC_REPORT_SCRIPT}")
    if missing:
        return False, "Missing tools: " + "; ".join(missing)
    return True, "PCB toolchain verified successfully"


# ---------------------------------------------------------------------------
# Makefile generation for KiCad DRC + preview
# ---------------------------------------------------------------------------

def create_makefile_for_pcb(build_dir, pcb_filename, dru_files,
                            output_prefix="board"):
    """
    Create a Makefile for the KiCad PCB DRC + preview pipeline.

    Args:
        build_dir:      Directory where the Makefile and all files live.
        pcb_filename:   Name of the student's .kicad_pcb file.
        dru_files:      List of dicts: [{"name": "min.kicad_dru",
                        "label": "Minimum"}, ...]
        output_prefix:  Prefix for output files (default: "board").
    """
    pcb_stem = os.path.splitext(pcb_filename)[0]
    sidecar_dru = f"{pcb_stem}.kicad_dru"

    # --- DRC targets ---
    drc_json_targets = []
    drc_html_targets = []
    drc_recipes = []

    for dru in dru_files:
        dru_name = dru["name"]
        dru_label = dru.get("label", dru_name)
        slug = os.path.splitext(dru_name)[0].replace(" ", "_").replace("-", "_")

        json_out = f"drc_{slug}.json"
        html_out = f"drc_{slug}.html"
        drc_json_targets.append(json_out)
        drc_html_targets.append(html_out)

        drc_recipes.append(f"""
# DRC: {dru_label}
{json_out}: {pcb_filename} {dru_name}
\t@echo "Running DRC ({dru_label})..."
\tcp -f {dru_name} {sidecar_dru}
\t-{KICAD_CLI} pcb drc --format json --output {json_out} {pcb_filename} 2>&1 || true
\t@# kicad-cli returns non-zero if violations found; we still want the report
\trm -f {sidecar_dru}

{html_out}: {json_out}
\t@echo "Generating HTML report ({dru_label})..."
\t{PYTHON} {DRC_REPORT_SCRIPT} {json_out} {html_out} --title "{dru_label}"
""")

    # --- SVG / PNG targets ---
    svg_targets = []
    png_targets = []
    svg_recipes = []

    for side, layers in PREVIEW_LAYERS.items():
        svg_name = f"preview_{side}.svg"
        png_name = f"preview_{side}.png"
        svg_targets.append(svg_name)
        png_targets.append(png_name)

        svg_recipes.append(f"""
{svg_name}: {pcb_filename}
\t@echo "Exporting {side} view..."
\t{KICAD_CLI} pcb export svg \\
\t\t--output {svg_name} \\
\t\t--layers {layers} \\
\t\t--page-size-mode 2 \\
\t\t--exclude-drawing-sheet \\
\t\t{pcb_filename}

{png_name}: {svg_name}
\t@echo "Converting {side} SVG to PNG..."
\t{RSVG_CONVERT} -d {PNG_DPI} -p {PNG_DPI} {svg_name} -o {png_name}
""")

    # --- Assemble Makefile ---
    all_targets = drc_html_targets + png_targets

    makefile_content = f"""# DALI — Auto-generated Makefile for KiCad PCB DRC + Preview
# PCB file: {pcb_filename}

# Tools
KICAD_CLI = {KICAD_CLI}
RSVG_CONVERT = {RSVG_CONVERT}
PYTHON = {PYTHON}

# Primary PCB file
PCB = {pcb_filename}

# All final outputs
ALL_OUTPUTS = {' '.join(all_targets)}

# Default target
all: $(ALL_OUTPUTS)
\t@echo "All DRC reports and previews generated."

{"".join(drc_recipes)}
{"".join(svg_recipes)}

# Clean
clean:
\t@echo "Cleaning..."
\trm -f {' '.join(drc_json_targets + drc_html_targets + svg_targets + png_targets)}
\trm -f {sidecar_dru}
\t@echo "Clean complete"

# Show configuration (for debugging)
config:
\t@echo "KiCad CLI: $(KICAD_CLI)"
\t@echo "PCB file:  $(PCB)"
\t@echo "DRU files: {' '.join(d['name'] for d in dru_files)}"
\t@echo "Outputs:   $(ALL_OUTPUTS)"

.PHONY: all clean config
"""

    makefile_path = os.path.join(build_dir, "Makefile")
    with open(makefile_path, "w") as f:
        f.write(makefile_content)
    return makefile_path


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
    """Parse a single S-expression from a token iterator."""
    result = []
    for tok in tokens:
        if tok == "(":
            result.append(_parse_sexpr(tokens))
        elif tok == ")":
            return result
        else:
            try:
                result.append(float(tok) if "." in tok else int(tok))
            except ValueError:
                result.append(tok)
    return result


def parse_kicad_pcb(pcb_path: Path) -> list:
    """Parse a .kicad_pcb file into nested lists."""
    text = Path(pcb_path).read_text(encoding="utf-8", errors="replace")
    tokens = _tokenize(text)
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
    """In a node like ['segment', ['start', 1, 2], ...], find the sub-list."""
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
        for child in node:
            if isinstance(child, list) and child[0] == "pts":
                for xy in child[1:]:
                    if isinstance(xy, list) and xy[0] == "xy":
                        update(xy[1], xy[2])

    # Also check footprint drawings on Edge.Cuts
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
    """
    results = []

    # Top-level gr_text items
    for node in _find_nodes(tree, "gr_text"):
        layer = _get_attr(node, "layer")
        if isinstance(layer, list):
            layer = layer[0]
        if layer not in COPPER_LAYERS:
            continue
        if len(node) >= 2 and isinstance(node[1], str):
            text = node[1]
            if text and text != "${REFERENCE}":
                results.append({"text": text, "layer": layer, "source": "graphic"})

    # fp_text inside footprints
    for fp in _find_nodes(tree, "footprint"):
        for node in _find_nodes(fp, "fp_text"):
            if len(node) < 3:
                continue
            text_type = node[1]
            text_val = node[2] if len(node) > 2 else ""

            layer = _get_attr(node, "layer")
            if isinstance(layer, list):
                layer = layer[0]
            if layer not in COPPER_LAYERS:
                continue

            if isinstance(text_val, str) and text_val.startswith("${"):
                continue

            if isinstance(text_val, str) and text_val.strip():
                results.append({
                    "text": text_val,
                    "layer": layer,
                    "source": f"footprint ({text_type})",
                })

    # gr_text_box (KiCad 8+)
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

def run_drc(pcb_path: Path, dru_path: Path,
            output_json: Path) -> tuple[bool, int]:
    """
    Run DRC on a PCB with a specific DRU file.

    Returns (passed: bool, error_count: int).
    """
    pcb_path = Path(pcb_path)
    dru_path = Path(dru_path)
    output_json = Path(output_json)

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
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)

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
