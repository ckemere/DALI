"""
PCB Makefile Generator for DALI — KiCad Edition

Generates Makefiles that run KiCad CLI for:
  1. DRC checks (one per DRU file, errors-only HTML reports)
  2. SVG export of top/bottom copper + silkscreen + edge cuts
  3. SVG → PNG conversion via rsvg-convert
"""

import os
import json


KICAD_CLI = os.environ.get("KICAD_CLI_PATH", "kicad-cli")
RSVG_CONVERT = os.environ.get("RSVG_CONVERT_PATH", "rsvg-convert")
PYTHON = os.environ.get("PYTHON_PATH", "python3")

# Path to the DRC report converter script (lives next to this file in deployment)
DRC_REPORT_SCRIPT = os.environ.get(
    "DRC_REPORT_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "drc_report_generator.py"),
)

PNG_DPI = int(os.environ.get("PCB_PREVIEW_DPI", "300"))

# Layer sets for preview export
PREVIEW_LAYERS = {
    "top": "F.Cu,Edge.Cuts,F.SilkS",
    "bottom": "B.Cu,Edge.Cuts,B.SilkS",
}


def create_makefile_for_pcb(build_dir, pcb_filename, dru_files, output_prefix="board"):
    """
    Create a Makefile for the KiCad PCB DRC + preview pipeline.

    Args:
        build_dir:      Directory where the Makefile and all files live.
        pcb_filename:   Name of the student's .kicad_pcb file (already in build_dir).
        dru_files:      List of dicts: [{"name": "min.kicad_dru", "label": "Minimum"}, ...]
        output_prefix:  Prefix for output files (default: "board").

    The Makefile does:
        1. For each DRU: copy it to match the PCB basename, run DRC, generate HTML
        2. Export top/bottom SVGs
        3. Convert SVGs to PNGs

    All outputs are written into build_dir.
    """

    pcb_stem = os.path.splitext(pcb_filename)[0]
    sidecar_dru = f"{pcb_stem}.kicad_dru"  # KiCad looks for this name

    # --- Build the list of DRC targets ---
    drc_json_targets = []
    drc_html_targets = []
    drc_recipes = []

    for dru in dru_files:
        dru_name = dru["name"]
        dru_label = dru.get("label", dru_name)
        # Derive a slug for output filenames
        slug = os.path.splitext(dru_name)[0].replace(" ", "_").replace("-", "_")

        json_out = f"drc_{slug}.json"
        html_out = f"drc_{slug}.html"
        drc_json_targets.append(json_out)
        drc_html_targets.append(html_out)

        # Recipe: copy DRU as sidecar → run DRC → generate HTML → remove sidecar
        drc_recipes.append(f"""
# DRC: {dru_label}
{json_out}: {pcb_filename} {dru_name}
\t@echo "🔍 Running DRC ({dru_label})..."
\tcp -f {dru_name} {sidecar_dru}
\t-{KICAD_CLI} pcb drc --format json --output {json_out} {pcb_filename} 2>&1 || true
\t@# kicad-cli returns non-zero if violations found; we still want the report
\trm -f {sidecar_dru}

{html_out}: {json_out}
\t@echo "📄 Generating HTML report ({dru_label})..."
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
\t@echo "📸 Exporting {side} view..."
\t{KICAD_CLI} pcb export svg \\
\t\t--output {svg_name} \\
\t\t--layers {layers} \\
\t\t--page-size-mode 2 \\
\t\t--exclude-drawing-sheet \\
\t\t{pcb_filename}

{png_name}: {svg_name}
\t@echo "🖼️  Converting {side} SVG → PNG..."
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
\t@echo "✅ All DRC reports and previews generated."

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


def verify_pcb_toolchain():
    """
    Verify that KiCad CLI and rsvg-convert are available.

    Returns:
        tuple: (success: bool, message: str)
    """
    import shutil

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


if __name__ == "__main__":
    import tempfile

    test_dir = tempfile.mkdtemp()
    print(f"Test directory: {test_dir}")

    ok, msg = verify_pcb_toolchain()
    print(f"Toolchain check: {msg}")

    dru_files = [
        {"name": "minimum_specs.kicad_dru", "label": "Minimum Manufacturing Specs"},
        {"name": "refined_specs.kicad_dru", "label": "Refined Design Specs"},
    ]

    makefile = create_makefile_for_pcb(test_dir, "board.kicad_pcb", dru_files)
    print(f"\nMakefile created: {makefile}")
    print("\n--- Generated Makefile ---")
    with open(makefile) as f:
        print(f.read())
