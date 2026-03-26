# Lab 4 Grading Guide

Lab 4 (PCB Design) grading combines **automated PCB analysis** (board area,
DRC, copper text) with **Canvas submission timestamps** to produce a final
score. The two data sources are generated independently and then merged by the
scoring script.

## Overview

```
  Canvas submission zips       Canvas API
  (KiCad .kicad_pcb files)     (assignment timestamps)
         |                           |
         v                           v
  +---------------+          +-------------------------+
  | grade_pcbs.py |          | fetch_submission_times   |
  +---------------+          +-------------------------+
         |                           |
    pcb_results.csv           presubmit_times.csv
         |                           |
         +-------------+-------------+
                       |
                       v
                +------------+
                | lab4/score |  <-- cutoff timestamp, max area
                +------------+
                       |
              lab4_grades.csv
                       |
                       v
                +--------------+
                | canvas.py    |  --> Canvas LMS
                +--------------+
```

## Prerequisites

| Tool / Package    | Purpose                                           |
| ----------------- | ------------------------------------------------- |
| Python 3.10+      | All scripts                                       |
| kicad-cli          | KiCad Design Rule Check (DRC); optional           |
| `pyyaml`          | Reading lab.yaml template config                  |
| `requests`        | Canvas API calls                                  |

**Environment variables** (set before running):

| Variable           | Required for                   | Example                    |
| ------------------ | ------------------------------ | -------------------------- |
| `CANVAS_API_TOKEN` | Fetching timestamps & upload   | `7~abc...`                 |
| `CANVAS_BASE_URL`  | Canvas API calls               | `https://canvas.rice.edu`  |
| `COURSE_ID`        | Fetching timestamps & upload   | `12345`                    |
| `KICAD_CLI_PATH`   | DRC (if not on PATH)           | `/usr/bin/kicad-cli`       |

---

## Step-by-Step Workflow

### Step 1: Download Submissions from Canvas

Download the student submission ZIP files from Canvas. Each ZIP contains the
student's KiCad PCB project (`.kicad_pcb`, `.kicad_sch`, etc.).

Place them all in a single directory, e.g., `./lab4_submissions/`.

Canvas names the ZIPs like:
```
studentname_12345_67890_Lab_4_abc12.zip
studentname_LATE_12345_67890_Lab_4_abc12-1.zip
```

The grader parses these filenames automatically and keeps only the latest
version per student.

### Step 2: Analyze PCB Designs

Run the PCB autograder against the submission ZIPs. This extracts each
`.kicad_pcb`, computes board dimensions, checks for copper-layer text
(student initials), and optionally runs KiCad DRC.

```bash
python grade_pcbs.py \
    ./lab4_submissions/ \
    template_files/lab4/ \
    -o pcb_results.csv
```

Arguments:
- First positional: directory of Canvas submission ZIPs
- Second positional: template directory containing `lab.yaml` and `.kicad_dru`
  rule files (e.g., `template_files/lab4/`)
- `-o` — output CSV path (default: `pcb_grades.csv`)
- `--no-drc` — skip DRC checks (faster; only compute dimensions and extract
  text)
- `--work-dir DIR` — use a persistent working directory instead of a temp dir

Output files:
- `pcb_results.csv` — one row per student with columns: `student_name`,
  `net_id`, `late`, `weak_drc_pass`, `weak_drc_errors`, `strong_drc_pass`,
  `strong_drc_errors`, `width_mm`, `height_mm`, `area_mm2`, `copper_texts`,
  `error`
- `pcb_results.html` — visual report with embedded PCB preview images and a
  sortable table

Open the HTML report to quickly spot-check results (board previews, DRC
status, copper text).

### Step 3: Fetch Pre-Submission Review Timestamps

Lab 4 awards a 10-point bonus for submitting a pre-review (Lab 4A) before the
deadline. Fetch submission timestamps from Canvas:

```bash
python fetch_submission_times.py <lab4a_assignment_id> -o presubmit_times.csv
```

Arguments:
- First positional: the Canvas assignment ID for the Lab 4A pre-submission
  review
- `-o` — output CSV path (default: stdout)

Output: `presubmit_times.csv` with columns: `student_name`, `canvas_id`,
`net_id`, `submitted_at`, `late`

### Step 4: Combine Scores

Merge the PCB analysis and pre-submission timestamps into final grades:

```bash
python -m grading.lab4.score \
    --pcb-csv pcb_results.csv \
    --presubmit-csv presubmit_times.csv \
    --cutoff "2025-03-15T14:15:00-05:00" \
    --max-area 1291 \
    -o lab4_grades.csv
```

Arguments:
- `--pcb-csv` — CSV from Step 2 (required)
- `--presubmit-csv` — CSV from Step 3 (optional; omit to skip bonus)
- `--cutoff` — ISO 8601 deadline for the pre-submission bonus (required if
  `--presubmit-csv` is provided)
- `--max-area` — maximum board area in mm² for full marks (default: 1291)
- `-o` — output CSV path (default: `lab4_grades.csv`)

Output: `lab4_grades.csv` with columns: `student_name`, `net_id`,
`pts_submission`, `pts_area`, `pts_initials`, `pts_weak_drc`,
`pts_presubmit_bonus`, `score`, `rubric_text`

The `rubric_text` column contains a human-readable breakdown suitable for
posting as a Canvas submission comment.

### Step 5: Upload Grades to Canvas

Use the generic Canvas upload tool:

```bash
python -m grading.canvas \
    --csv lab4_grades.csv \
    --assignment-id <lab4_assignment_id> \
    --student-column net_id \
    --score-column score \
    --comment-column rubric_text
```

Use `--dry-run` first to preview:

```bash
python -m grading.canvas \
    --csv lab4_grades.csv \
    --assignment-id <lab4_assignment_id> \
    --student-column net_id \
    --score-column score \
    --comment-column rubric_text \
    --dry-run
```

---

## Rubric

| Item                  | Points | Criteria                                      |
| --------------------- | -----: | --------------------------------------------- |
| Submission            |     70 | Student submitted a `.kicad_pcb` file          |
| Board area            |     10 | Bounding-box area ≤ max (default 1291 mm²)    |
| Initials on copper    |     10 | Text found on F.Cu or B.Cu layer              |
| Weak DRC pass         |     10 | No errors under the weak design rule check     |
| Pre-submission bonus  |     10 | Lab 4A submitted before the cutoff deadline    |
| **Total**             | **110** | (100 base + 10 bonus)                         |

### DRC Rule Sets

The template directory (`template_files/lab4/`) contains two `.kicad_dru` rule
files configured in `lab.yaml`:

- **weak.kicad_dru** — Relaxed rules; used for the graded 10-point rubric item.
  Students should pass this with a reasonable design.
- **strong.kicad_dru** — Stricter rules; reported in the CSV for informational
  purposes but not graded.

---

## File Reference

| File                                | Purpose                                      |
| ----------------------------------- | -------------------------------------------- |
| `grading/lab4/grade_pcbs.py`        | PCB analysis: area, DRC, copper text, HTML report |
| `grading/lab4/score.py`             | Combine PCB analysis + timestamps into grades |
| `grading/fetch_submission_times.py` | Fetch submission timestamps from Canvas API  |
| `grading/canvas.py`                 | Generic Canvas grade upload                  |
| `template_files/lab4/lab.yaml`      | Lab config (DRU file list, display name)     |
| `template_files/lab4/*.kicad_dru`   | KiCad design rule files (weak, strong)       |

---

## Quick Reference

```bash
# Full pipeline in four commands:

# 1. Analyze PCBs
python grade_pcbs.py ./submissions/ template_files/lab4/ -o pcb_results.csv

# 2. Fetch pre-submission timestamps
python fetch_submission_times.py 506143 -o presubmit_times.csv

# 3. Combine into final grades
python -m grading.lab4.score \
    --pcb-csv pcb_results.csv \
    --presubmit-csv presubmit_times.csv \
    --cutoff "2025-03-15T14:15:00-05:00" \
    -o lab4_grades.csv

# 4. Upload to Canvas
python -m grading.canvas \
    --csv lab4_grades.csv \
    --assignment-id 506200 \
    --student-column net_id \
    --score-column score \
    --comment-column rubric_text
```
