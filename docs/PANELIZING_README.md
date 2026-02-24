# panelize_class.py

Automated PCB panelization for a class set of KiCad student submissions downloaded from Canvas. Parses submission zips, deduplicates by student (keeping the latest version), bin-packs boards into panels, adds tabbed connections with mouse bites via KiKit, and exports Gerbers and drill files.

## Prerequisites

- **KiCad 8+** (with `kicad-cli` and `pcbnew` Python bindings)
- **Python 3** (the one bundled with KiCad)
- **KiKit 1.7.x** (`pip install kikit`)

## Installation

Create a virtual environment using KiCad's Python so that `pcbnew` is available:

```bash
# macOS example — adjust the path for your KiCad installation
PYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3

${PYTHON} -m venv --system-site-packages venv-ki
./venv-ki/bin/pip3 install kikit
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `KICAD_CLI_PATH` | Yes | Full path to the `kicad-cli` binary. On macOS: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` |

```bash
export KICAD_CLI_PATH="/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
```

## Usage

```bash
./venv-ki/bin/python3 panelize_class.py submissions/ -o panel_output/
```

where `submissions/` is a directory of Canvas-downloaded zip files. Each zip should contain a `Lab4.kicad_pcb` file.

### Command-Line Options

```
positional arguments:
  zip_dir                Directory containing Canvas submission zip files

options:
  -o, --output DIR       Output directory (default: ./panel_output)
  --panel-width MM       Max panel width in mm (default: 254 = 10")
  --panel-height MM      Max panel height in mm (default: 304.8 = 12")
  --spacing MM           Spacing between boards in mm (default: 3)
  --frame-width MM       Rail width on top/bottom panel edges in mm (default: 5)
  --tab-width MM         Width of tabs between boards in mm (default: 3)
  --mouse-bite-dia MM    Mouse bite hole diameter in mm (default: 0.5)
  --mouse-bite-spacing MM  Mouse bite hole spacing in mm (default: 1.0)
  --no-gerbers           Skip Gerber export
```

### Example

```bash
./venv-ki/bin/python3 panelize_class.py submissions/ \
    -o panel_output/ \
    --spacing 3 \
    --frame-width 5 \
    --tab-width 3 \
    --mouse-bite-dia 0.5 \
    --mouse-bite-spacing 1.0
```

## Output Structure

```
panel_output/
├── work/                          # Extracted student submissions
├── panels/
│   ├── panel_1.kicad_pcb          # Panel KiCad file
│   ├── panel_1_drills/
│   │   ├── student_npth/          # NPTH drills from student designs only
│   │   └── all_npth/              # Student NPTH + mouse bite holes
│   ├── panel_2.kicad_pcb
│   └── panel_2_drills/
│       └── ...
├── gerbers/
│   ├── panel_1/
│   │   ├── *-F_Cu.gbr             # Front copper + board outlines (reference)
│   │   ├── *-B_Cu.gbr             # Back copper
│   │   ├── *-Edge_Cuts.gbr        # Panel rectangle (send to fab)
│   │   ├── *-Eco1_User.gbr        # Full substrate outline with tabs (CNC milling)
│   │   ├── *.drl                  # Drill files
│   │   └── ...
│   └── panel_2/
│       └── ...
├── panel_1_map.svg                # Visual reference map with student IDs
└── panel_2_map.svg
```

## Gerber Layer Guide

| Gerber Layer | Contents | Purpose |
|---|---|---|
| **Edge.Cuts** | Panel outer rectangle | Send to PCB fab |
| **F.Cu** | Board outlines only (no tabs/rails) | Copper layer reference |
| **B.Cu** | Back copper traces | Copper layer |
| **Eco1.User** | Full substrate: board outlines + tabs + rails | CNC milling toolpath |

## Drill File Guide

Two sets of NPTH drill files are exported per panel — before and after mouse bites are added. The diff between them isolates the mouse bite holes from any student-placed NPTH holes.

| Directory | Contents | Purpose |
|---|---|---|
| `student_npth/` | Student NPTH holes only | Fab or reference |
| `all_npth/` | Student NPTH + mouse bites | Full drill set |

## How It Works

1. **Parse** Canvas submission zips, handling `LATE` tags and version numbers. Deduplicates by student, keeping the latest version.
2. **Extract** each zip and locate `Lab4.kicad_pcb`.
3. **Read bounding boxes** via `pcbnew` to get each board's dimensions.
4. **Bin-pack** boards into panels using a shelf algorithm (sorted by height, with rotation). Boards that exceed the panel size are skipped with a warning.
5. **Build panels** via KiKit: append boards, add top/bottom rails as separate substrates (with a gap for tabbed connections), then create tabs only between neighboring boards/rails. Tabs are only placed when substrate is found on both sides.
6. **Mouse bites** are added along tab cut lines.
7. **Post-process** to separate Edge.Cuts (panel rectangle for fab), F.Cu (clean board outlines), and Eco1.User (full substrate for CNC).
8. **Export** Gerbers and drill files via `kicad-cli`.
9. **Generate** SVG reference maps showing board placement and student IDs.

## Canvas Filename Format

The script expects Canvas-style zip filenames:

```
lastname_firstname_netid_12345_67890_Lab4-1.zip
lastname_firstname_netid_12345_67890_Lab4.zip       (no version)
LATE_lastname_firstname_netid_12345_67890_Lab4-2.zip (late submission)
```
