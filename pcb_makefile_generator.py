"""
PCB Makefile Generator for DALI — KiCad Edition

Thin wrapper — canonical implementation is in assess/pcb.py.
"""

# Re-export everything so existing consumers (compile_queue.py, etc.) work.
from assess.pcb import (  # noqa: F401
    KICAD_CLI,
    RSVG_CONVERT,
    PYTHON,
    DRC_REPORT_SCRIPT,
    PNG_DPI,
    PREVIEW_LAYERS,
    verify_pcb_toolchain,
    create_makefile_for_pcb,
)


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
