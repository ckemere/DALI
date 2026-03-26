# Lab 1 Grading Guide

Lab 1 (LED Clock) grading uses a multi-stage pipeline that combines **video
analysis** of the running PCB with **AI code review** of the student's source
files.  The pipeline produces per-student grade reports and uploads scores to
Canvas.

## Overview

```
 Student .zip files           calibration.json
        |                          |
        v                          v
  +-----------+  compile   +-----------------+
  | grade.py  | ---------> | flash to board  |
  +-----------+  (TI ARM)  +-----------------+
        |                          |
        |                     record video
        |                     (ffmpeg)
        |                          |
        v                          v
  +-------------+          +------------------+
  | code_review | (Gemini) | video_analyzer   |
  +-------------+          +------------------+
        |                          |
        v                          v
  llm_results.json         video_results.json
        |                          |
        +------------+-------------+
                     |
                     v
            +----------------+
            | score_results  |  <-- rubric.yaml (point weights)
            +----------------+
                     |
            +--------+--------+
            |                 |
            v                 v
       grades.csv      reports/*.txt
            |
            v
      +---------------+
      | canvas_upload  |  --> Canvas LMS
      +---------------+
```

## Prerequisites

| Tool / Package        | Purpose                                      |
| --------------------- | -------------------------------------------- |
| Python 3.10+          | All scripts                                  |
| TI ARM Clang          | Compiling student C code                     |
| DSLite                | Flashing firmware to MSPM0G3507 LaunchPad    |
| ffmpeg                | Recording video of the running board         |
| USB webcam            | Pointed at the LED clock board               |
| `opencv-python`       | Video frame analysis                         |
| `numpy`               | LED brightness detection                     |
| `google-genai`        | Gemini API for AI code review                |
| `pyyaml`              | Rubric weight configuration                  |
| `requests`            | Canvas API uploads                           |

**Environment variables** (set before running):

| Variable            | Required for           | Example                         |
| ------------------- | ---------------------- | ------------------------------- |
| `GEMINI_API_KEY`    | Code review            | `AIza...`                       |
| `CANVAS_API_TOKEN`  | Canvas upload          | `7~abc...`                      |
| `CANVAS_BASE_URL`   | Canvas upload          | `https://canvas.rice.edu`       |
| `COURSE_ID`         | Canvas upload          | `12345`                         |
| `DSLITE_PATH`       | Flashing (if not auto) | `/opt/ti/ccs/dslite/dslite.sh`  |

---

## Step-by-Step Workflow

### Step 1: Calibrate the Camera

Before grading, calibrate the camera/LED board setup. This records the pixel
positions and brightness thresholds for each of the 24 LEDs (12 inner ring, 12
outer ring) plus the debug LED.

```bash
# From a live camera feed:
python calibrate_lab1.py --camera 0 --output calibration.json

# Or from a pre-recorded reference video:
python calibrate_lab1.py --video reference.mp4 --output calibration.json
```

In the GUI window:
1. Click on the **debug LED** first.
2. Click on each of the **12 outer ring** LEDs in order (0-11).
3. Click on each of the **12 inner ring** LEDs in order (0-11).
4. Use `+`/`-` keys to adjust the brightness threshold.
5. Press `s` to save and exit.

Output: `calibration.json`

### Step 2: Collect Videos of Student Submissions

This step compiles each student's code, flashes it to the board, and records a
video of the LEDs running. There are several modes depending on your setup.

#### Option A: Full automated pipeline (compile + flash + record)

```bash
python grade_lab1.py \
    --submissions-dir ./submissions/ \
    --ccxml MSPM0G3507.ccxml \
    --calibration calibration.json \
    --video-dir ./videos/ \
    --video-duration 150 \
    --results-csv raw_results.csv
```

This processes every `.zip` in `submissions/`, one at a time:
1. Extracts the zip
2. Copies infrastructure files (startup code, linker script) from `template_files/lab1/`
3. Compiles with TI ARM Clang via Makefile
4. Flashes to the board via DSLite
5. Records video via ffmpeg
6. Analyzes the video using the calibration data

#### Option B: Debug a single student

```bash
python grade_lab1.py \
    --zip submissions/student_12345_67890_Lab_1_abc12.zip \
    --ccxml MSPM0G3507.ccxml \
    --calibration calibration.json \
    --keep-build
```

#### Option C: Analyze pre-recorded videos only (no hardware needed)

If you already have videos recorded (e.g., from a TA session), analyze them
in batch:

```bash
python grade_lab1.py \
    --analyze-dir ./videos/ \
    --calibration calibration.json \
    --results-csv video_scores.csv
```

### Step 3: Run the Combined Grading Pipeline

The `--grade-batch` mode runs both **video analysis** and **LLM code review**
in one pass, producing two JSON files:

```bash
python grade_lab1.py \
    --grade-batch ./submissions/ \
    --video-dir ./videos/ \
    --calibration calibration.json \
    --video-output video_results.json \
    --llm-output llm_results.json \
    --bulk 2
```

Key flags:
- `--bulk N` — Send all students to Gemini in a single request, repeated N
  times with shuffled order to check consistency. Much faster than per-student
  mode.
- `--skip-video` — Skip video analysis (LLM only).
- `--skip-llm` — Skip LLM review (video only).
- `--model gemini-2.5-flash` — Choose the Gemini model (default:
  `gemini-2.5-flash`).
- `--verbose-llm` — Print full prompts and responses for debugging.

Output:
- `video_results.json` — Per-student video rubric scores
- `llm_results.json` — Per-student code review verdicts with reasons and evidence

### Step 4: Review and Adjust Scores with the Rubric

#### 4a. Export and edit the rubric

```bash
python -m grading.lab1.score_results --export-rubric rubric.yaml
```

This creates a YAML file with default point weights for each rubric item. Edit
the `points` values to adjust weighting:

```yaml
video_rubric:
  - id: distinct_rings
    description: Inner and outer rings activate separately
    points: 1
  - id: timing_1hz
    description: LED timing is approximately 1 Hz
    points: 1
  # ... more items ...

code_rubric:
  - id: design_doc_present
    description: Design document is present
    points: 1
  - id: safe_read_modify_write
    description: Uses safe read-modify-write for GPIO
    points: 1
  # ... more items ...
```

#### 4b. Generate grades CSV and reports

```bash
python -m grading.lab1.score_results \
    --video-results video_results.json \
    --llm-results llm_results.json \
    --rubric rubric.yaml \
    --grades-csv grades.csv \
    --reports-dir reports/
```

Output:
- `grades.csv` — One row per student with per-item points, subtotals, and
  grand total.
- `reports/` — One `<student>_report.txt` per student with detailed feedback
  including video measurements and code review findings with evidence.

### Step 5: Upload Grades to Canvas

#### Option A: Lab 1 upload with feedback bundles (report + video)

```bash
python -m grading.lab1.canvas_upload \
    --csv grades.csv \
    --reports-dir reports/ \
    --video-dir videos/ \
    --course-id 12345 \
    --assignment-id 67890
```

This will:
1. Fetch the Canvas roster to match student names to Canvas user IDs
2. Create a feedback ZIP for each student (report + video)
3. Upload the grade and attach the feedback ZIP as a submission comment

Use `--dry-run` first to preview without uploading:

```bash
python -m grading.lab1.canvas_upload \
    --csv grades.csv \
    --reports-dir reports/ \
    --video-dir videos/ \
    --dry-run
```

#### Option B: Generic CSV upload (scores only, any lab)

```bash
python -m grading.canvas \
    --csv grades.csv \
    --assignment-id 67890 \
    --student-column net_id \
    --score-column "grand_total (max 24)" \
    --comment-column rubric_text
```

---

## Rubric Items

### Video Analysis (scored automatically from recorded video)

| Item                        | Description                                     |
| --------------------------- | ----------------------------------------------- |
| `distinct_rings`            | Inner and outer rings activate separately        |
| `all_24_leds_seen`          | All 24 LEDs observed active at some point        |
| `timing_1hz`                | LED transitions occur at ~1 Hz                   |
| `inner_clockwise_sequence`  | Inner ring advances clockwise                    |
| `outer_clockwise_sequence`  | Outer ring advances clockwise                    |
| `inner_sequence_wrap`       | Inner ring wraps from position 11 back to 0      |
| `outer_sequence_wrap`       | Outer ring wraps from position 11 back to 0      |
| `full_clock_cycle`          | A full 12-step clock cycle is observed           |
| `hour_increment_at_wrap`    | Outer ring (hour) increments when inner wraps    |

### Code Review (scored by Gemini AI)

| Item                          | Description                                    |
| ----------------------------- | ---------------------------------------------- |
| `design_doc_present`          | Design document is included                    |
| `diagram_included`            | Block/state diagram in the design doc          |
| `state_machine_explanation`   | Design doc explains state machine logic        |
| `writeup_matches_code`        | Design doc matches the submitted code          |
| `code_commentary`             | Code contains meaningful comments              |
| `power_reset_gpio`            | Enables power and resets GPIO peripherals       |
| `iomux_configuration`         | Configures IOMUX for LED pins                  |
| `output_enable_doe`           | Sets DOE (direction output enable) bits        |
| `safe_read_modify_write`      | Uses read-modify-write (not blind writes)      |
| `gpio_state_initialization`   | Initializes GPIO output state                  |
| `init_completeness_24_leds`   | Initialization covers all 24 LEDs              |
| `led_activation_logic`        | Correct logic for activating individual LEDs   |
| `infinite_loop`               | Main loop runs indefinitely                    |
| `data_structure_state_machine`| Uses a data structure for state machine        |
| `timing_delay`                | Implements a timing delay (~1 second)          |

---

## Standalone Utilities

### Analyze a single video

```bash
python analyze_lab1_video.py video.mp4 calibration.json
```

Shows rubric scores and a replay of the first 30 LED state changes.

### Run code review on a single submission

```bash
# From a zip:
python -m grading.lab1.review_cli --zip student.zip

# From an extracted directory:
python -m grading.lab1.review_cli --dir ./extracted/

# Batch mode (all zips in a directory):
python -m grading.lab1.review_cli --batch ./submissions/ --results-csv review.csv
```

---

## File Reference

| File                            | Purpose                                         |
| ------------------------------- | ----------------------------------------------- |
| `grading/lab1/grade.py`         | Main orchestrator (compile, flash, record, analyze, review) |
| `grading/lab1/score.py`         | Video timeline scoring (rubric logic)           |
| `grading/lab1/analyze.py`       | Standalone video analysis CLI                   |
| `grading/lab1/code_review.py`   | Gemini-based AI code review                     |
| `grading/lab1/review_cli.py`    | CLI for single/batch code review                |
| `grading/lab1/score_results.py` | Combine results, apply rubric weights, generate reports |
| `grading/lab1/canvas_upload.py` | Upload grades + feedback bundles to Canvas      |
| `grading/video_analyzer.py`     | OpenCV LED detection from video frames          |
| `grading/calibrate.py`          | Interactive GUI for LED board calibration       |
| `grading/build_utils.py`        | Compile, flash, and recording utilities         |
| `grading/canvas.py`             | Shared Canvas API helpers                       |
