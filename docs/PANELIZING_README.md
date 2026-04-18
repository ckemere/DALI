# panelize_pcbs.py

Automated PCB panelization for a class set of KiCad student submissions downloaded from Canvas. Supports two workflows:

1. **Automatic packing** — Parse Canvas zips, extract boards, bin-pack using optimized Guillotine algorithms, and build panels.
2. **Custom JSON layout** — Load a manually-curated JSON panel layout (with subPanels, custom rotations, and guillotine-cuttable structure) to override automatic packing.

Both workflows build panels with KiKit, add tabbed connections and mouse bites, and export Gerbers and drill files.

## Prerequisites

- **KiCad 8+** (with `kicad-cli` and `pcbnew` Python bindings)
- **Python 3.8+** (preferably KiCad's bundled Python)
- **KiKit 1.7.x** (`pip install kikit`)
- **rectpack** (`pip install rectpack`) — for optimized bin-packing

## Installation

Create a virtual environment using KiCad's Python so that `pcbnew` is available:

```bash
# macOS example — adjust the path for your KiCad installation
PYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3

${PYTHON} -m venv --system-site-packages venv-ki
./venv-ki/bin/pip3 install -r requirements.txt
```

For Linux, use the system Python if KiCad bindings are installed:
```bash
python3 -m venv --system-site-packages venv-ki
./venv-ki/bin/pip3 install kikit rectpack
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `KICAD_CLI_PATH` | Yes | Full path to the `kicad-cli` binary. On macOS: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` |

```bash
export KICAD_CLI_PATH="/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
```

## Usage

### Workflow 1: Automatic Packing from Canvas Zips

```bash
./venv-ki/bin/python3 panelize_pcbs.py submissions/ -o panel_output/
```

where `submissions/` is a directory of Canvas-downloaded zip files.

**Generates:** Panel KiCad files + JSON specifications showing the computed layout.

### Workflow 2: Custom JSON Layout

After auto-packing, edit the generated JSON files (`json/panel_0.json`, `json/panel_1.json`, etc.) to customize layout with:
- **Subpanels** — group PCBs into reusable modules (rows of rows)
- **Rotations** — specify 0, 90, 180, or 270° rotation per PCB
- **Spacing** — control gaps between items and rows

Then rebuild using the custom layout:

```bash
./venv-ki/bin/python3 panelize_pcbs.py submissions/ --json json/panel_0.json -o panel_output/
```

### Command-Line Options

```
positional arguments:
  zip_dir                Directory containing Canvas submission zip files
                         (optional if --json is provided)

options:
  -o, --output DIR       Output directory (default: ./panel_output)
  --json FILE            Use custom JSON panel layout instead of auto-packing
  --panel-width MM       Max panel width in mm (default: 254 = 10")
  --panel-height MM      Max panel height in mm (default: 304.8 = 12")
  --spacing MM           Gap between adjacent boards in mm (default: 3)
  --frame-width MM       Rail width on top/bottom/sides in mm (default: 5)
  --tab-width MM         Width of tabs between boards in mm (default: 3)
  --mouse-bite-dia MM    Mouse bite hole diameter in mm (default: 0.5)
  --mouse-bite-spacing MM  Mouse bite hole spacing in mm (default: 1.0)
  --packing-algo ALGO    Guillotine algorithm variant (default: GuillotineBssfMaxas)
  --try-all-algos        Try all 18 Guillotine variants with multiple board
                         orderings and pick the best result (slower but better packing)
  --no-gerbers           Skip Gerber export
```

### Examples

**Auto-pack with comprehensive algorithm search:**
```bash
./venv-ki/bin/python3 panelize_pcbs.py submissions/ --try-all-algos -o panel_output/
```

**Auto-pack with specific algorithm:**
```bash
./venv-ki/bin/python3 panelize_pcbs.py submissions/ --packing-algo GuillotineBlsfMaxas -o panel_output/
```

**Build with custom JSON layout:**
```bash
./venv-ki/bin/python3 panelize_pcbs.py submissions/ --json custom_layout.json -o panel_output/
```

## JSON Layout Format

Each panel gets a separate JSON file (`panel_0.json`, `panel_1.json`, etc.). Example:

```json
{
  "rows": [
    [
      {"netid": "hz111"},
      {
        "subpanel": {
          "rows": [
            [
              {"netid": "hz108", "rotation": 0},
              {"netid": "hz109"}
            ],
            [
              {"netid": "hz110", "rotation": 90}
            ]
          ]
        }
      },
      {"netid": "hz112", "rotation": 90}
    ],
    [
      {"netid": "hz113"}
    ]
  ]
}
```

**Key features:**
- **Rows:** Ordered top-to-bottom with spacing between rows
- **Items:** PCBs or subPanels
  - **PCB item:** `{"netid": "...", "rotation": 0}` (rotation optional, degrees)
  - **SubPanel item:** `{"subpanel": {"rows": [...]}}` (nested structure)
- **SubPanels:** Organize PCBs into guillotine-cuttable groups
- **Spacing:** Automatically applied between items and rows (no edge margins)

## Output Structure

```
panel_output/
├── work/                          # Extracted student submissions
├── json/                          # Auto-generated JSON panel layouts (editable)
│   ├── panel_0.json               # Layout blueprint for panel 0
│   ├── panel_1.json               # Layout blueprint for panel 1
│   └── ...
├── panels/
│   ├── panel_0.kicad_pcb          # Panel KiCad file
│   ├── panel_0_drills/
│   │   ├── student_npth/          # NPTH drills from student designs only
│   │   └── all_npth/              # Student NPTH + mouse bite holes
│   ├── panel_1.kicad_pcb
│   └── panel_1_drills/
│       └── ...
├── gerbers/
│   ├── panel_0/
│   │   ├── *-F_Cu.gbr             # Front copper + board outlines (reference)
│   │   ├── *-B_Cu.gbr             # Back copper
│   │   ├── *-Edge_Cuts.gbr        # Panel rectangle (send to fab)
│   │   ├── *-Eco1_User.gbr        # Full substrate outline with tabs (CNC milling)
│   │   ├── *.drl                  # Drill files
│   │   └── ...
│   └── panel_1/
│       └── ...
├── panel_0_map.svg                # Visual reference map with student IDs
├── panel_1_map.svg
└── ...
```

**JSON files are editable** — modify them to customize board placement, add subPanels, or adjust rotations, then re-run with `--json` to rebuild panels.

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

### Automatic Packing Workflow

1. **Parse** Canvas submission zips, handling `LATE` tags and version numbers. Deduplicates by student, keeping the latest version.
2. **Extract** each zip and locate the `.kicad_pcb` file.
3. **Read bounding boxes** via `pcbnew` to get each board's dimensions.
4. **Bin-pack** boards into panels using RectPack's Guillotine algorithms:
   - By default, uses `GuillotineBssfMaxas` for tight packing
   - With `--try-all-algos`, tests all 18 variants with multiple board orderings and picks the best
   - Supports rotation to find optimal placement
5. **Generate JSON** specs showing the computed layout (editable for manual refinement).
6. **Build panels** via KiKit: append boards, add top/bottom rails as separate substrates (with gaps for tabbed connections), create tabs only between neighboring boards/rails.
7. **Add mouse bites** along tab cut lines.
8. **Post-process** to separate layers: Edge.Cuts (panel rectangle), F.Cu (clean board outlines), Eco1.User (full substrate with tabs for CNC milling).
9. **Export** Gerbers and drill files via `kicad-cli`.
10. **Generate** SVG reference maps showing board placement and student IDs.

### Custom JSON Workflow

1. Edit the auto-generated JSON files to customize layout:
   - Rearrange PCBs and create subPanels
   - Add custom rotations
   - Group boards into guillotine-cuttable structures
2. Re-run with `--json panel_0.json` to rebuild panels using your custom layout.
3. Repeat steps 6-10 above.

## RectPack Guillotine Algorithms

Rectpack provides 18 Guillotine algorithm variants combining:

**Section selection criteria:**
- **BSSF** — Best Short Side Fit
- **BLSF** — Best Long Side Fit
- **BAF** — Best Area Fit

**Split rules:**
- **SAS** — Short Axis Split
- **LAS** — Long Axis Split
- **SLAS** — Short Leftover Axis Split
- **LLAS** — Long Leftover Axis Split
- **MAXAS** — Max Area Axis Split
- **MINAS** — Min Area Axis Split

**Recommended variants:**
- `GuillotineBssfMaxas` (default) — tight packing, good all-around
- `GuillotineBlsfMaxas` — tight packing, handles varied sizes well
- `GuillotineBafSas` — fastest for large numbers of boards

Use `--try-all-algos` to automatically test all variants and board orderings.

## Canvas Filename Format

The script expects Canvas-style zip filenames:

```
lastname_firstname_netid_12345_67890_Lab4-1.zip
lastname_firstname_netid_12345_67890_Lab4.zip       (no version)
LATE_lastname_firstname_netid_12345_67890_Lab4-2.zip (late submission)
```
