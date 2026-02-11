#!/usr/bin/env python3
"""
Manual dry-run test for the PCB Makefile generator.

Uses your actual template directory (with real .kicad_pcb, .kicad_dru, lab.yaml)
to generate a Makefile and run `make -n`.

Usage:
    python test_pcb_dryrun.py template_files/lab4
    python test_pcb_dryrun.py template_files/lab4 --keep
"""

import os
import sys
import shutil
import tempfile
import subprocess
import yaml

from pcb_makefile_generator import create_makefile_for_pcb


def main():
    if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
        print(f"Usage: {sys.argv[0]} <template_dir> [--keep]")
        print(f"  e.g. {sys.argv[0]} template_files/lab4")
        sys.exit(1)

    template_dir = sys.argv[1]
    keep = "--keep" in sys.argv

    if not os.path.isdir(template_dir):
        print(f"Error: {template_dir} is not a directory")
        sys.exit(1)

    # Load lab.yaml
    yaml_path = os.path.join(template_dir, "lab.yaml")
    if os.path.isfile(yaml_path):
        with open(yaml_path) as f:
            meta = yaml.safe_load(f)
        dru_configs = meta.get("dru_files", [])
        print(f"Loaded lab.yaml: {meta.get('display_name', '?')}")
        print(f"  DRU files: {[d['name'] for d in dru_configs]}")
    else:
        print(f"No lab.yaml found — scanning for .kicad_dru files")
        dru_configs = [
            {"name": f, "label": os.path.splitext(f)[0]}
            for f in sorted(os.listdir(template_dir))
            if f.endswith(".kicad_dru")
        ]

    # Find PCB file
    pcb_files = [f for f in os.listdir(template_dir) if f.endswith(".kicad_pcb")]
    if not pcb_files:
        print(f"Error: no .kicad_pcb file found in {template_dir}")
        sys.exit(1)
    pcb_name = pcb_files[0]
    print(f"  PCB file:  {pcb_name}")
    print()

    # Copy everything into a temp build directory
    build_dir = tempfile.mkdtemp(prefix="dali_pcb_dryrun_")
    print(f"Build directory: {build_dir}")

    for fname in os.listdir(template_dir):
        src = os.path.join(template_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(build_dir, fname))

    # Generate Makefile
    makefile_path = create_makefile_for_pcb(build_dir, pcb_name, dru_configs)

    # Show the Makefile
    print()
    print("=" * 60)
    print("GENERATED MAKEFILE")
    print("=" * 60)
    with open(makefile_path) as f:
        print(f.read())

    # Dry run
    print("=" * 60)
    print("make -n all")
    print("=" * 60)
    result = subprocess.run(
        ["make", "-n", "-C", build_dir, "all"],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    print(f"Exit code: {result.returncode}")

    if keep:
        print(f"\n--keep: temp dir preserved at {build_dir}")
        print(f"  To run for real: make -C {build_dir} all")
    else:
        shutil.rmtree(build_dir)
        print(f"\nCleaned up. Use --keep to preserve the build dir and try `make` for real.")


if __name__ == "__main__":
    main()
