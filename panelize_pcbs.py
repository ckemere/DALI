#!/usr/bin/env python3
"""
panelize_class.py — Panelize a class set of KiCad PCB submissions from Canvas.

Workflow:
  1. Parse Canvas zip submissions, keep latest version per student
  2. Extract .kicad_pcb files into per-student directories
  3. Compute bounding boxes
  4. Bin-pack into panels (constrained to max panel size)
  5. Build each panel using KiKit's Python API
  6. Copy Edge.Cuts outlines to F.Cu (for maskless/screenless fab)
  7. Export Gerbers (F.Cu, B.Cu, Edge.Cuts, drills)
  8. Generate SVG reference map with student names

Usage:
  python panelize_class.py /path/to/canvas/zips/ -o /path/to/output/

  Run with KiCad's Python interpreter (or a venv with --system-site-packages):
    macOS:  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
    Linux:  python3 (if pcbnew is importable)

Requirements:
  - KiCad 8+
  - KiKit: pip install kikit
"""

import os
import re
import sys
import shutil
import zipfile
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# KiCad / KiKit imports (deferred so --help works without them)
# ---------------------------------------------------------------------------
pcbnew = None
kikit_panel = None

def _import_kicad():
    global pcbnew, kikit_panel
    try:
        import pcbnew as _pcbnew
        pcbnew = _pcbnew
    except ImportError:
        sys.exit(
            "ERROR: Cannot import pcbnew. Run this script with KiCad's Python:\n"
            "  macOS: /Applications/KiCad/KiCad.app/Contents/Frameworks/"
            "Python.framework/Versions/Current/bin/python3\n"
            "  Linux: ensure kicad python bindings are installed"
        )
    try:
        from kikit import panelize as _panelize
        kikit_panel = _panelize
    except ImportError:
        sys.exit("ERROR: Cannot import kikit. Install with: pip install kikit")


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class StudentBoard:
    """One student's PCB submission."""
    student_name: str       # e.g. "zhuharry"
    net_id: str             # e.g. "hz108"
    version: int            # Canvas resubmission index
    zip_path: Path
    pcb_path: Optional[Path] = None   # set after extraction
    width_mm: float = 0.0
    height_mm: float = 0.0

@dataclass
class Placement:
    """A board placed on a panel."""
    board: StudentBoard
    x_mm: float             # center x on panel
    y_mm: float             # center y on panel
    rotated: bool = False   # True if rotated 90°

@dataclass
class Panel:
    """One fabrication panel."""
    index: int
    placements: list = field(default_factory=list)
    width_mm: float = 0.0
    height_mm: float = 0.0


# ===========================================================================
# 1. Parse & extract Canvas submissions
# ===========================================================================

# Canvas zip filename patterns:
#   studentname_canvasid_submissionid_Assignment_Name_netid-version.zip
#   studentname_canvasid_submissionid_Assignment_Name_netid.zip  (first submission, no version)
#   studentname_LATE_canvasid_submissionid_Assignment_Name_netid-version.zip
ZIP_PATTERN = re.compile(
    r'^(?P<student>.+?)_(?:LATE_)?(?P<canvasid>\d+)_(?P<subid>\d+)_(?P<assignment>.+)_(?P<netid>[a-zA-Z0-9]+)(?:-(?P<version>\d+))?\.zip$'
)

def parse_submissions(zip_dir: Path) -> list[StudentBoard]:
    """Parse Canvas zip filenames, group by student, keep latest version."""
    by_student: dict[str, StudentBoard] = {}

    for f in sorted(zip_dir.iterdir()):
        if not f.name.endswith('.zip'):
            continue
        m = ZIP_PATTERN.match(f.name)
        if not m:
            print(f"  WARNING: Skipping unrecognized zip: {f.name}")
            continue

        student = m.group('student')
        net_id = m.group('netid')
        version = int(m.group('version')) if m.group('version') else 0

        if student not in by_student or version > by_student[student].version:
            by_student[student] = StudentBoard(
                student_name=student,
                net_id=net_id,
                version=version,
                zip_path=f,
            )

    boards = sorted(by_student.values(), key=lambda b: b.student_name)
    print(f"Found {len(boards)} unique students (from {sum(1 for f in zip_dir.iterdir() if f.name.endswith('.zip'))} zips)")
    return boards


def extract_submissions(boards: list[StudentBoard], work_dir: Path) -> list[StudentBoard]:
    """Unzip each student's latest submission; locate .kicad_pcb file."""
    extracted = []
    for board in boards:
        student_dir = work_dir / board.net_id
        student_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(board.zip_path, 'r') as zf:
            zf.extractall(student_dir)

        # Find the .kicad_pcb file
        pcb_files = list(student_dir.glob('*.kicad_pcb'))
        if not pcb_files:
            print(f"  WARNING: No .kicad_pcb in {board.zip_path.name}, skipping")
            continue
        if len(pcb_files) > 1:
            print(f"  WARNING: Multiple .kicad_pcb files for {board.net_id}, using {pcb_files[0].name}")

        board.pcb_path = pcb_files[0]
        extracted.append(board)

    print(f"Extracted {len(extracted)} boards")
    return extracted


# ===========================================================================
# 2. Read bounding boxes
# ===========================================================================

def read_bounding_boxes(boards: list[StudentBoard]) -> list[StudentBoard]:
    """Use pcbnew to read each board's Edge.Cuts bounding box."""
    valid = []
    for board in boards:
        try:
            b = pcbnew.LoadBoard(str(board.pcb_path))
            bbox = b.GetBoardEdgesBoundingBox()
            board.width_mm = pcbnew.ToMM(bbox.GetWidth())
            board.height_mm = pcbnew.ToMM(bbox.GetHeight())

            if board.width_mm <= 0 or board.height_mm <= 0:
                print(f"  WARNING: {board.net_id} has zero-size bbox, skipping")
                continue

            print(f"  {board.net_id}: {board.width_mm:.1f} x {board.height_mm:.1f} mm")
            valid.append(board)
        except Exception as e:
            print(f"  WARNING: Failed to read {board.net_id}: {e}")
    return valid


# ===========================================================================
# 3. Bin packing (shelf-based with rotation)
# ===========================================================================

def bin_pack_panels(
    boards: list[StudentBoard],
    panel_w: float,
    panel_h: float,
    spacing: float,
    frame_w: float,
) -> list[Panel]:
    """
    Pack boards into panels using Shelf Next-Fit Decreasing Height.

    Each board's footprint on the panel is its bbox + spacing on all sides.
    The usable area inside the panel is reduced by the frame width.
    """
    usable_w = panel_w - 2 * frame_w
    usable_h = panel_h - 2 * frame_w

    # For each board, compute the space it needs (bbox + spacing on each side)
    @dataclass
    class PackItem:
        board: StudentBoard
        w: float        # width including spacing
        h: float        # height including spacing
        rotated: bool = False

    items = []
    oversized = []
    for b in boards:
        w = b.width_mm + 2 * spacing
        h = b.height_mm + 2 * spacing

        # Try both orientations, see if it fits at all
        fits_normal = (w <= usable_w and h <= usable_h)
        fits_rotated = (h <= usable_w and w <= usable_h)

        if not fits_normal and not fits_rotated:
            oversized.append(b)
            continue

        # Prefer orientation where width <= usable_w (better shelf packing)
        if fits_normal:
            items.append(PackItem(board=b, w=w, h=h, rotated=False))
        else:
            items.append(PackItem(board=b, w=h, h=w, rotated=True))

    if oversized:
        print(f"\n  WARNING: {len(oversized)} boards too large for panel:")
        for b in oversized:
            print(f"    {b.net_id}: {b.width_mm:.1f} x {b.height_mm:.1f} mm")

    # Sort by height descending (classic shelf heuristic)
    items.sort(key=lambda it: it.h, reverse=True)

    panels: list[Panel] = []
    current_panel = Panel(index=0)
    shelf_y = 0.0       # top of current shelf (y offset within usable area)
    shelf_h = 0.0       # height of current shelf
    shelf_x = 0.0       # current x position on shelf

    def new_panel():
        nonlocal current_panel, shelf_y, shelf_h, shelf_x
        if current_panel.placements:
            panels.append(current_panel)
        current_panel = Panel(index=len(panels))
        shelf_y = 0.0
        shelf_h = 0.0
        shelf_x = 0.0

    def new_shelf(item_h):
        nonlocal shelf_y, shelf_h, shelf_x
        shelf_y += shelf_h
        shelf_h = item_h
        shelf_x = 0.0

    for item in items:
        # Does it fit on the current shelf?
        if shelf_x + item.w <= usable_w and shelf_y + max(shelf_h, item.h) <= usable_h:
            pass  # fits on current shelf
        elif shelf_y + shelf_h + item.h <= usable_h:
            # Start a new shelf on this panel
            new_shelf(item.h)
        else:
            # Need a new panel
            new_panel()
            shelf_y = 0.0
            shelf_h = 0.0
            shelf_x = 0.0

        # Update shelf height if this item is taller
        if item.h > shelf_h:
            shelf_h = item.h

        # Place the board — coordinates are center of the board within the panel
        # (frame_w offset + spacing + half board dimension)
        cx = frame_w + shelf_x + item.w / 2
        cy = frame_w + shelf_y + item.h / 2

        current_panel.placements.append(Placement(
            board=item.board,
            x_mm=cx,
            y_mm=cy,
            rotated=item.rotated,
        ))

        shelf_x += item.w

    # Don't forget the last panel
    if current_panel.placements:
        panels.append(current_panel)

    # Compute actual panel dimensions
    for p in panels:
        max_x = max(pl.x_mm + (pl.board.height_mm if pl.rotated else pl.board.width_mm) / 2 + spacing
                     for pl in p.placements) + frame_w
        max_y = max(pl.y_mm + (pl.board.width_mm if pl.rotated else pl.board.height_mm) / 2 + spacing
                     for pl in p.placements) + frame_w
        p.width_mm = min(max_x, panel_w)
        p.height_mm = min(max_y, panel_h)

    return panels


# ===========================================================================
# 4. Build panels with KiKit
# ===========================================================================

def build_tabs_between_neighbors(p, panel, spacing, tab_width, frame_w):
    """
    Create tabs only between adjacent boards or between boards and rails.
    For each board edge, check if there's a neighbor within reach. If so,
    shoot tab() from the gap midpoint in both directions to get cuts on
    both ends (for mouse bites at each connection).

    Returns the list of cut LineStrings for makeMouseBites().
    """
    from kikit.units import mm

    tw = int(tab_width * mm)
    max_reach = int((spacing + frame_w + 5) * mm)

    # Build bounding rects for all boards
    board_rects = []
    for pl in panel.placements:
        bw = pl.board.height_mm if pl.rotated else pl.board.width_mm
        bh = pl.board.width_mm if pl.rotated else pl.board.height_mm
        board_rects.append({
            'left': pl.x_mm - bw / 2,
            'right': pl.x_mm + bw / 2,
            'top': pl.y_mm - bh / 2,
            'bottom': pl.y_mm + bh / 2,
            'cx': pl.x_mm,
            'cy': pl.y_mm,
        })

    # Rail rects for neighbor detection.
    # makeRailsTb adds rails at the very top/bottom of the merged substrate.
    # The boards are offset by frame_w, so rails are approximately:
    #   Top rail:    y from min_board_top - spacing - rail_thickness to min_board_top - spacing
    #   Bottom rail: y from max_board_bottom + spacing to max_board_bottom + spacing + rail_thickness
    # For simplicity, use the panel dimensions: rails span the full width
    # at y=0..frame_w and y=(height-frame_w)..height
    rail_rects = [
        {'left': 0, 'right': panel.width_mm,
         'top': 0, 'bottom': frame_w},
        {'left': 0, 'right': panel.width_mm,
         'top': panel.height_mm - frame_w, 'bottom': panel.height_mm},
    ]

    all_rects = board_rects + rail_rects

    all_tabs = []
    all_cuts = []
    skipped = 0

    for idx, rect in enumerate(board_rects):
        # Four edges: midpoint + outward direction
        edges = [
            (rect['cx'], rect['top'],    0, -1),  # top edge, pointing up
            (rect['cx'], rect['bottom'], 0,  1),  # bottom edge, pointing down
            (rect['left'],  rect['cy'], -1,  0),  # left edge, pointing left
            (rect['right'], rect['cy'],  1,  0),  # right edge, pointing right
        ]

        for mx, my, dx, dy in edges:
            # Check if there's a neighbor in this direction
            neighbor_found = False
            for j, other in enumerate(all_rects):
                if j == idx:
                    continue

                if dx == 0:  # vertical (top/bottom edge)
                    # Need horizontal overlap between board and neighbor
                    h_overlap = (min(rect['right'], other['right'])
                                 - max(rect['left'], other['left']))
                    if h_overlap <= 0:
                        continue
                    if dy == -1:  # looking up
                        gap = rect['top'] - other['bottom']
                    else:  # looking down
                        gap = other['top'] - rect['bottom']
                else:  # horizontal (left/right edge)
                    v_overlap = (min(rect['bottom'], other['bottom'])
                                 - max(rect['top'], other['top']))
                    if v_overlap <= 0:
                        continue
                    if dx == -1:  # looking left
                        gap = rect['left'] - other['right']
                    else:  # looking right
                        gap = other['left'] - rect['right']

                if -1 < gap < spacing * 2 + frame_w + 2:
                    neighbor_found = True
                    break

            if not neighbor_found:
                continue

            # Shoot from gap midpoint in BOTH directions to get cuts at both ends
            gap_x = mx + dx * spacing / 2
            gap_y = my + dy * spacing / 2
            gap_x_nm = int(gap_x * mm)
            gap_y_nm = int(gap_y * mm)

            pair_tabs = []
            pair_cuts = []
            pair_ok = True

            for shoot_dx, shoot_dy in [(dx, dy), (-dx, -dy)]:
                try:
                    tab_shape, cut_line = p.boardSubstrate.tab(
                        origin=(gap_x_nm, gap_y_nm),
                        direction=(shoot_dx, shoot_dy),
                        width=tw,
                        maxHeight=max_reach,
                    )
                    if tab_shape is None:
                        pair_ok = False
                        break
                    pair_tabs.append(tab_shape)
                    if cut_line is not None:
                        pair_cuts.append(cut_line)
                except Exception:
                    # No substrate found in this direction within reach
                    pair_ok = False
                    break

            if pair_ok and len(pair_tabs) == 2:
                all_tabs.extend(pair_tabs)
                all_cuts.extend(pair_cuts)
            else:
                skipped += 1

    # Batch-add all tab shapes to the panel substrate
    for t in all_tabs:
        p.appendSubstrate(t)

    n_tabs = len(all_tabs)
    n_cuts = len(all_cuts)
    print(f"    Created {n_tabs} tab segments, {n_cuts} cut lines ({skipped} edges skipped — no neighbor on both sides)")

    return all_cuts


def build_panel_pcb(
    panel: Panel,
    output_path: Path,
    frame_w: float,
    spacing: float,
    tab_width: float,
    mouse_bite_dia: float,
    mouse_bite_spacing: float,
):
    """Build a .kicad_pcb panel file using KiKit's Python API.

    Workflow:
      1. Append boards, add rails, build tabs
      2. Save → export NPTH drills (student holes only)
      3. Add mouse bites
      4. Save → export NPTH drills (student + mouse bites)
    The diff between the two NPTH files = mouse bite holes.
    """
    from kikit.panelize import Panel as KiKitPanel, Origin
    from kikit.units import mm

    p = KiKitPanel(str(output_path))

    # Inherit design settings from the first board
    first_pcb_path = str(panel.placements[0].board.pcb_path)
    first_board = pcbnew.LoadBoard(first_pcb_path)
    p.inheritDesignSettings(first_board)
    p.inheritProperties(first_board)
    p.inheritCopperLayers(first_board)

    # Append each board at its computed position
    for pl in panel.placements:
        origin = pcbnew.VECTOR2I(
            pcbnew.FromMM(pl.x_mm),
            pcbnew.FromMM(pl.y_mm),
        )
        rotation = pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T) if pl.rotated else pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T)

        print(f"    Appending {pl.board.net_id} at ({pl.x_mm:.1f}, {pl.y_mm:.1f})...")
        try:
            p.appendBoard(
                str(pl.board.pcb_path),
                origin,
                origin=Origin.Center,
                rotationAngle=rotation,
                tolerance=pcbnew.FromMM(5),
                inheritDrc=False,
            )
        except Exception as e:
            print(f"    ERROR appending {pl.board.net_id}: {e}")
            print(f"    Skipping this board and continuing...")
            continue

    # Add top/bottom rails as separate substrates with a gap,
    # so our tab logic can create tabbed (not fused) connections.
    # makeRailsTb would fuse rails to adjacent boards — we don't want that.
    from shapely.geometry import box as shapely_box
    rail_thickness = int(frame_w * mm)
    print(f"    Adding top/bottom rails ({frame_w}mm, with gap for tabs)...")

    rail_x_right = int(panel.width_mm * mm)
    top_rail = shapely_box(0, 0, rail_x_right, rail_thickness)
    bottom_rail = shapely_box(
        0, int((panel.height_mm - frame_w) * mm),
        rail_x_right, int(panel.height_mm * mm),
    )
    p.appendSubstrate(top_rail)
    p.appendSubstrate(bottom_rail)

    # Build tabs only between adjacent boards/rails
    print(f"    Building tabs ({tab_width}mm wide, neighbors only)...")
    cuts = build_tabs_between_neighbors(p, panel, spacing, tab_width, frame_w)

    # --- First save: panel with tabs, NO mouse bites ---
    p.save(str(output_path))
    print(f"  Saved panel PCB (pre-mouse-bites): {output_path}")

    # Export NPTH drills before mouse bites (student holes only)
    drill_dir = output_path.parent / f"{output_path.stem}_drills"
    drill_dir.mkdir(parents=True, exist_ok=True)
    export_drills(output_path, drill_dir / "student_npth")

    # --- Add mouse bites ---
    print(f"    Adding mouse bites (dia={mouse_bite_dia}mm, spacing={mouse_bite_spacing}mm)...")
    p.makeMouseBites(
        cuts,
        diameter=int(mouse_bite_dia * mm),
        spacing=int(mouse_bite_spacing * mm),
    )

    # --- Second save: panel WITH mouse bites ---
    p.save(str(output_path))

    # Post-process: separate outlines onto three layers
    add_panel_outline(output_path, panel.width_mm, panel.height_mm)
    add_board_outlines_to_copper(output_path, panel.placements)
    print(f"  Saved panel PCB (with mouse bites): {output_path}")

    # Export NPTH drills after mouse bites (student + mouse bites)
    export_drills(output_path, drill_dir / "all_npth")

    print(f"  Drill files: {drill_dir}")
    print(f"    student_npth/ = student NPTH holes only")
    print(f"    all_npth/     = student NPTH + mouse bites")

    return output_path


def export_drills(panel_path: Path, output_dir: Path):
    """Export just drill files using kicad-cli."""
    output_dir.mkdir(parents=True, exist_ok=True)
    kicad_cli = os.environ.get("KICAD_CLI_PATH", "kicad-cli")

    cmd = [
        kicad_cli, "pcb", "export", "drill",
        "--output", str(output_dir) + "/",
        "--format", "excellon",
        str(panel_path),
    ]
    print(f"    Exporting drills: {output_dir.name}")
    subprocess.run(cmd, check=True)


def add_panel_outline(panel_path: Path, width_mm: float, height_mm: float):
    """
    Post-process the saved panel to separate outlines onto three layers:
      - Eco1.User: full KiKit substrate (boards + tabs + rails) for CNC milling
      - F.Cu: clean board outlines only (no tabs/rails) for copper reference
      - Edge.Cuts: panel outer rectangle for fab
    """
    board = pcbnew.LoadBoard(str(panel_path))

    # Step 1: Move ALL KiKit Edge.Cuts → Eco1.User (milling layer)
    for drawing in list(board.GetDrawings()):
        if drawing.GetLayer() == pcbnew.Edge_Cuts:
            drawing.SetLayer(pcbnew.Eco1_User)

    # Step 2: Draw the panel outer rectangle on Edge.Cuts
    SEGMENT = getattr(pcbnew, 'SHAPE_T_SEGMENT', None) or getattr(pcbnew, 'S_SEGMENT', None)
    if SEGMENT is None:
        SEGMENT = 0

    corners = [
        (0, 0),
        (width_mm, 0),
        (width_mm, height_mm),
        (0, height_mm),
    ]
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        line = pcbnew.PCB_SHAPE(board)
        line.SetShape(SEGMENT)
        line.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        line.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
        line.SetLayer(pcbnew.Edge_Cuts)
        line.SetWidth(pcbnew.FromMM(0.1))
        board.Add(line)

    board.Save(str(panel_path))


def add_board_outlines_to_copper(panel_path: Path, placements):
    """
    Re-derive clean board outlines (no tabs/rails) from the original source
    .kicad_pcb files and place them onto F.Cu at each board's panel position.
    """
    board = pcbnew.LoadBoard(str(panel_path))

    for pl in placements:
        src = pcbnew.LoadBoard(str(pl.board.pcb_path))
        src_bbox = src.GetBoardEdgesBoundingBox()
        src_cx = src_bbox.GetCenter().x
        src_cy = src_bbox.GetCenter().y

        for drawing in src.GetDrawings():
            if drawing.GetLayer() == pcbnew.Edge_Cuts:
                clone = drawing.Duplicate()
                # Translate from source center to panel position
                dx = pcbnew.FromMM(pl.x_mm) - src_cx
                dy = pcbnew.FromMM(pl.y_mm) - src_cy
                clone.Move(pcbnew.VECTOR2I(dx, dy))
                # Rotate around the panel placement point if needed
                if pl.rotated:
                    center = pcbnew.VECTOR2I(
                        pcbnew.FromMM(pl.x_mm),
                        pcbnew.FromMM(pl.y_mm),
                    )
                    clone.Rotate(center, pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T))
                clone.SetLayer(pcbnew.F_Cu)
                board.Add(clone)

    board.Save(str(panel_path))
    print(f"  Copied clean board outlines → F.Cu ({len(placements)} boards)")


# ===========================================================================
# 6. Export Gerbers
# ===========================================================================

def export_gerbers(panel_path: Path, output_dir: Path):
    """Export Gerbers + drill files using kicad-cli."""
    gerber_dir = output_dir / panel_path.stem
    gerber_dir.mkdir(parents=True, exist_ok=True)

    kicad_cli = os.environ.get("KICAD_CLI_PATH", "kicad-cli")

    # Export Gerbers (only the layers we need)
    layers = "F.Cu,B.Cu,Edge.Cuts,Eco1.User"
    cmd_gerber = [
        kicad_cli, "pcb", "export", "gerbers",
        "--output", str(gerber_dir) + "/",
        "--layers", layers,
        str(panel_path),
    ]
    print(f"  Exporting Gerbers: {' '.join(cmd_gerber)}")
    subprocess.run(cmd_gerber, check=True)

    # Export drill files
    cmd_drill = [
        kicad_cli, "pcb", "export", "drill",
        "--output", str(gerber_dir) + "/",
        "--format", "excellon",
        str(panel_path),
    ]
    print(f"  Exporting drills: {' '.join(cmd_drill)}")
    subprocess.run(cmd_drill, check=True)

    print(f"  Gerbers saved to: {gerber_dir}")
    return gerber_dir


# ===========================================================================
# 7. Generate SVG reference map
# ===========================================================================

def generate_reference_svg(panel: Panel, output_path: Path, frame_w: float, spacing: float):
    """Generate an SVG showing board outlines with student names/netids."""
    scale = 3.0  # pixels per mm
    svg_w = panel.width_mm * scale
    svg_h = panel.height_mm * scale

    lines = []
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" '
                 f'width="{svg_w:.0f}" height="{svg_h:.0f}" '
                 f'viewBox="0 0 {panel.width_mm:.1f} {panel.height_mm:.1f}">')
    lines.append(f'<style>')
    lines.append(f'  text {{ font-family: monospace; font-size: 2.5px; fill: #333; text-anchor: middle; }}')
    lines.append(f'  .label {{ font-size: 1.8px; fill: #666; }}')
    lines.append(f'  rect.board {{ fill: #e8f5e9; stroke: #2e7d32; stroke-width: 0.3; }}')
    lines.append(f'  rect.panel {{ fill: none; stroke: #333; stroke-width: 0.5; }}')
    lines.append(f'</style>')

    # Panel outline
    lines.append(f'<rect class="panel" x="0" y="0" '
                 f'width="{panel.width_mm:.1f}" height="{panel.height_mm:.1f}" />')

    # Each board
    for pl in panel.placements:
        bw = pl.board.height_mm if pl.rotated else pl.board.width_mm
        bh = pl.board.width_mm if pl.rotated else pl.board.height_mm
        rx = pl.x_mm - bw / 2
        ry = pl.y_mm - bh / 2

        lines.append(f'<rect class="board" x="{rx:.2f}" y="{ry:.2f}" '
                     f'width="{bw:.2f}" height="{bh:.2f}" />')

        # Student name (net_id)
        lines.append(f'<text x="{pl.x_mm:.2f}" y="{pl.y_mm - 1:.2f}">'
                     f'{pl.board.net_id}</text>')
        # Dimensions
        lines.append(f'<text class="label" x="{pl.x_mm:.2f}" y="{pl.y_mm + 2:.2f}">'
                     f'{pl.board.width_mm:.0f}×{pl.board.height_mm:.0f}</text>')

    lines.append('</svg>')

    output_path.write_text('\n'.join(lines))
    print(f"  Reference map: {output_path}")


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Panelize a class set of KiCad PCB submissions from Canvas."
    )
    parser.add_argument("zip_dir", type=Path,
                        help="Directory containing Canvas submission zip files")
    parser.add_argument("-o", "--output", type=Path, default=Path("./panel_output"),
                        help="Output directory (default: ./panel_output)")
    parser.add_argument("--panel-width", type=float, default=254.0,
                        help="Max panel width in mm (default: 254 = 10in)")
    parser.add_argument("--panel-height", type=float, default=304.8,
                        help="Max panel height in mm (default: 304.8 = 12in)")
    parser.add_argument("--spacing", type=float, default=3.0,
                        help="Spacing between boards in mm (default: 3)")
    parser.add_argument("--frame-width", type=float, default=5.0,
                        help="Frame/rail width around panel edge in mm (default: 5)")
    parser.add_argument("--no-gerbers", action="store_true",
                        help="Skip Gerber export")
    parser.add_argument("--tab-width", type=float, default=3.0,
                        help="Width of tabs between boards in mm (default: 3)")
    parser.add_argument("--mouse-bite-dia", type=float, default=0.5,
                        help="Mouse bite hole diameter in mm (default: 0.5)")
    parser.add_argument("--mouse-bite-spacing", type=float, default=1.0,
                        help="Mouse bite hole spacing in mm (default: 1.0)")
    args = parser.parse_args()

    print("=" * 60)
    print("DALI PCB Panelization")
    print("=" * 60)

    # --- Phase 1: Parse & extract (pure Python) ---
    print(f"\n[1/7] Parsing submissions from {args.zip_dir}")
    boards = parse_submissions(args.zip_dir)

    work_dir = args.output / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[2/7] Extracting latest submissions to {work_dir}")
    boards = extract_submissions(boards, work_dir)

    if not boards:
        sys.exit("ERROR: No valid submissions found!")

    # --- Phase 2: Read bboxes (needs pcbnew) ---
    print(f"\n[3/7] Reading board dimensions...")
    _import_kicad()
    boards = read_bounding_boxes(boards)

    if not boards:
        sys.exit("ERROR: No valid boards after reading dimensions!")

    # --- Phase 3: Bin pack ---
    print(f"\n[4/7] Packing into panels ({args.panel_width:.0f} x {args.panel_height:.0f} mm)...")
    panels = bin_pack_panels(boards, args.panel_width, args.panel_height,
                             args.spacing, args.frame_width)

    for p in panels:
        print(f"\n  Panel {p.index + 1}: {len(p.placements)} boards")
        for pl in p.placements:
            rot_str = " (rotated)" if pl.rotated else ""
            print(f"    {pl.board.net_id}: at ({pl.x_mm:.1f}, {pl.y_mm:.1f}){rot_str}")

    # --- Phase 4: Build panels (with rails, tabs, mouse bites) ---
    print(f"\n[5/7] Building panel PCBs with tabs + mouse bites...")
    panel_dir = args.output / "panels"
    panel_dir.mkdir(parents=True, exist_ok=True)

    panel_paths = []
    for p in panels:
        panel_path = panel_dir / f"panel_{p.index + 1}.kicad_pcb"
        try:
            build_panel_pcb(
                p, panel_path, args.frame_width, args.spacing,
                tab_width=args.tab_width,
                mouse_bite_dia=args.mouse_bite_dia,
                mouse_bite_spacing=args.mouse_bite_spacing,
            )
            panel_paths.append((p, panel_path))
        except Exception as e:
            print(f"  ERROR building panel {p.index + 1}: {e}")
            print(f"  You may need to adjust the KiKit API calls for your version.")
            raise

    # --- Phase 5: Export Gerbers ---
    if not args.no_gerbers:
        print(f"\n[6/7] Exporting Gerbers...")
        gerber_dir = args.output / "gerbers"
        for p, path in panel_paths:
            export_gerbers(path, gerber_dir)
    else:
        print(f"\n[6/7] Skipping Gerber export (--no-gerbers)")

    # --- Phase 6: Generate reference SVGs ---
    print(f"\n[7/7] Generating reference maps...")
    for p in panels:
        svg_path = args.output / f"panel_{p.index + 1}_map.svg"
        generate_reference_svg(p, svg_path, args.frame_width, args.spacing)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Students processed: {len(boards)}")
    print(f"  Panels created:     {len(panels)}")
    for p in panels:
        w_in = p.width_mm / 25.4
        h_in = p.height_mm / 25.4
        print(f"    Panel {p.index + 1}: {len(p.placements)} boards, "
              f"{p.width_mm:.1f} x {p.height_mm:.1f} mm "
              f"({w_in:.2f} x {h_in:.2f} in)")
    print(f"\n  Output directory: {args.output}")
    print(f"  Panel PCBs:       {panel_dir}")
    if not args.no_gerbers:
        print(f"  Gerbers:          {args.output / 'gerbers'}")
        print(f"    F.Cu        = board outlines (clean, no tabs)")
        print(f"    Edge.Cuts   = panel rectangle (for fab)")
        print(f"    Eco1.User   = full substrate outline (for CNC milling)")
    print(f"  Drill files:      {panel_dir}/panel_*_drills/")
    print(f"    student_npth/ = student NPTH holes only")
    print(f"    all_npth/     = student NPTH + mouse bites")
    print(f"  Reference maps:   {args.output}/panel_*_map.svg")
    print()


if __name__ == "__main__":
    main()

    