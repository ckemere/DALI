# Lab 2 Grading Guide

Lab 2 (Timer Interrupts & Power Savings) grading uses a multi-stage pipeline
that combines **video analysis** of the running PCB with **AI code review** of
the student's source files and writeup.  Three firmware phases are graded
together.

## Overview

Lab 2 has three firmware phases, each submitted as a separate Canvas
assignment:

| Phase   | Canvas Assignment | Description                           |
| ------- | ----------------- | ------------------------------------- |
| Phase 1 | lab2-1 (510245)   | Lab 1 recapitulation (busy-wait)      |
| Phase 2 | lab2-2 (510246)   | Timer interrupt + standby sleep       |
| Phase 3 | lab2-3 (510247)   | PWM LED brightness + writeup          |

Students upload a zip of 5 code files per phase.  The writeup (PDF or TXT) is
submitted only with Phase 3.

```
 Phase 1 zips     Phase 2 zips     Phase 3 zips + writeup
      |                |                |
      v                v                v
 +-----------+   +-----------+   +-----------+
 | compile   |   | compile   |   | compile   |
 | flash     |   | flash     |   | flash     |
 | record    |   | record    |   | record    |
 +-----------+   +-----------+   +-----------+
      |                |                |
      v                v                v
 Lab 1 video     Lab 1 video     Lab 1 video
  scoring         scoring        scoring + PWM analysis
      |                |                |
      +-------+--------+-------+--------+
              |                |
              v                v
      video_results.json  llm_results.json
              |                |
              +-------+--------+
                      |
                      v
             +----------------+
             | score_results  |  <-- rubric.yaml
             +----------------+
                      |
             +--------+--------+
             |                 |
             v                 v
        grades.csv      reports/*.txt
             |
             v
       +---------------+
       | canvas_upload |  --> Canvas LMS
       +---------------+
```

## Prerequisites

- The DALI repo cloned somewhere (referred to below as `$DALI_ROOT`)
- A Python virtualenv with the DALI requirements installed (`numpy`, `opencv`,
  `google-genai`, `PyYAML`, etc.)
- TI ARM Clang compiler (`tiarmclang`) and the MSPM0 SDK installed
- DSLite (`DSLite` from CCS) on `PATH` or pointed at by `DSLITE_PATH`
- `ffmpeg` and `ffplay` installed (Homebrew on macOS, apt on Linux)
- A Gemini API key in `GEMINI_API_KEY` (for the LLM code review step only)
- A camera capable of at least 30 fps at 640×480.  A built-in laptop webcam
  is fine — the analyzer has a low-fps fallback for Phase 3 PWM detection.

## Step-by-Step Workflow

The instructions below assume you are running from a working directory
**outside** the DALI repo, e.g. `~/lab2_grading/`.

### Step 0 — Lay out submissions

Download the submission zip files from Canvas into three sibling
directories:

```
~/lab2_grading/
  phase1_submissions/     # zips from Canvas assignment 510245
  phase2_submissions/     # zips from Canvas assignment 510246
  phase3_submissions/     # zips from Canvas assignment 510247
```

### Step 1 — Activate the environment

The grading code lives in `$DALI_ROOT` but you don't have to `cd` there.
Set up your shell once per session:

```bash
cd ~/lab2_grading

# 1. Activate your venv (adjust path)
source ~/dali-venv/bin/activate

# 2. Tell Python where to find the grading modules
export DALI_ROOT=/path/to/DALI
export PYTHONPATH="$DALI_ROOT"

# 3. Load DALI's .env (TI paths, DSLITE_PATH, GEMINI_API_KEY)
set -a
source "$DALI_ROOT/.env"
set +a
```

You can stash all of this in a `lab2_env.sh` script alongside your
submissions and just `source lab2_env.sh` each session.

Sanity check:

```bash
python -c "import grading.lab2.grade; print(grading.lab2.grade.__file__)"
```

This should print a path inside `$DALI_ROOT/grading/lab2/grade.py`.

### Step 2 — Verify camera framing

Aim the camera at the LED board and make sure all 24 LEDs are visible at
the resolution the grader will use (640×480 by default).  Live preview
with `ffplay`:

```bash
# macOS — substitute your camera index from
# `ffmpeg -f avfoundation -list_devices true -i ""`
ffplay -f avfoundation -framerate 30 -video_size 640x480 -i "0"

# Linux
ffplay -f v4l2 -input_format mjpeg -video_size 640x480 \
       -framerate 30 -i /dev/video0
```

Adjust focus and aim until:
- All 24 LEDs are visible inside the frame with a little margin
- Focus is sharp enough to distinguish individual LEDs
- No serious vignetting in the corners where LEDs sit

Then **don't move the camera again** until after Step 4 (calibration).
The calibration depends on the LEDs being in the same pixel locations
as in the captured videos.

### Step 3 — Compile, flash, and record videos

```bash
python -m grading.lab2.grade --capture \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --ccxml "$DALI_ROOT/MSPM0G3507.ccxml" \
    --video-dir ./videos \
    --keep-builds ./builds \
    --results-csv capture_results.csv
```

This processes each student's three phases sequentially:

1. Extracts the zip
2. Copies infrastructure files from `template_files/lab2-{1,2,3}/`
3. Compiles with TI ARM Clang
4. Flashes to the board via DSLite
5. Records video at the per-phase frame rate

Default capture settings:

| Phase   | FPS | Resolution | Duration |
| ------- | --- | ---------- | -------- |
| Phase 1 |  5  | 640×480    | 150 s    |
| Phase 2 |  5  | 640×480    | 150 s    |
| Phase 3 | 30  | 640×480    | 150 s    |

Phase 1/2 only need to capture the 1 Hz LED clock so 5 fps is plenty
(well above the 2 Hz Nyquist).  Phase 3 records at 30 fps; the analyzer
will infer PWM from brightness reduction at this rate.  Override any of
these with `--phase1-fps`, `--phase2-fps`, `--phase3-fps`,
`--phase3-duration`, or `--video-size`.

Videos land in `./videos/phase1/`, `./videos/phase2/`, `./videos/phase3/`,
named `<student>.mp4`.

The grader prints the full ffmpeg command it runs at the start of every
recording, so you can confirm the negotiated frame rate and pixel
format.  After a single student, sanity check one video:

```bash
ffprobe -v error -select_streams v:0 \
    -show_entries stream=avg_frame_rate,r_frame_rate,width,height \
    -of default=nw=1 ./videos/phase1/<student>.mp4
```

You should see something like `r_frame_rate=5/1`, `avg_frame_rate=5/1`,
`width=640`, `height=480`.

#### Why `--keep-builds`?

`--keep-builds DIR` writes each student/phase's compiled `.out` (and
`.map`, intermediate `.o`) files to `DIR/<student>/<phase>/` instead of
a temp directory.  This is useful when:

- A flash fails and you want to re-flash by hand to see DSLite's full
  output:
  ```bash
  $DSLITE_PATH load -c "$DALI_ROOT/MSPM0G3507.ccxml" \
      -f ./builds/<student>/phase3/Lab_2_3.out
  ```
- You want to disassemble or inspect a particular student's binary
- A grading run was interrupted and you don't want to recompile
  everything from scratch

The `capture_results.csv` produced by the run includes a `build_dir`
column pointing at the right directory for each row, plus a
`flash_errors` column with the first lines of DSLite stderr/stdout for
any failed flash.

#### Compile-only mode (no hardware needed)

```bash
python -m grading.lab2.grade --capture \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --ccxml "$DALI_ROOT/MSPM0G3507.ccxml" \
    --compile-only \
    --keep-builds ./builds \
    --results-csv compile_results.csv
```

#### High-speed camera (optional)

If you have a camera that supports 120+ fps at low resolution, the
Phase 3 analyzer can use FFT-based PWM frequency estimation:

```bash
python -m grading.lab2.grade --capture \
    --video-size 320x240 \
    --phase3-fps 120 \
    ...
```

Without this, Phase 3 falls back to brightness-reduction PWM detection
which works at any frame rate.

#### Robustness to partial submissions

Students who only submitted some of the three phases are handled
gracefully — they're matched across phase directories by name, missing
phases are skipped (with a `no submission` log line) and missing data
becomes `NO_DATA` → 0 points downstream.

### Step 4 — Calibrate the camera

After Step 3 finishes (and **with the camera still in the same
position**), capture a calibration:

```bash
python -m grading.calibrate --camera 0 --output ./calibration.json
```

This saves the LED ring positions and brightness thresholds that the
analyzer uses to find each LED in the frame.

### Step 5 — Analyze pre-recorded videos

```bash
python -m grading.lab2.grade \
    --analyze-videos ./videos \
    --calibration ./calibration.json \
    --video-output video_results.json
```

Expects `./videos/{phase1,phase2,phase3}/<student>.mp4`.

- Phase 1 and Phase 2 are scored with the standard Lab 1
  clock-behavior rubric (timing, sequence, wrapping, etc.).
- Phase 3 is scored with the same Lab 1 clock-behavior rubric **plus**
  PWM-specific items (`pwm_detected`, `reduced_brightness`,
  `no_visible_flicker`).

The PWM analyzer auto-detects the frame rate from the video and uses:
- **Low-fps mode** (fps < 60): infers PWM from brightness reduction;
  uses CV at the camera's frame rate as a flicker proxy (smooth = PWM
  above the human flicker fusion threshold; ripply = visible flicker).
- **High-fps mode** (fps ≥ 60): uses CV/FFT directly to measure PWM
  frequency and duty cycle.

### Step 6 — Run LLM code review

```bash
python -m grading.lab2.grade --code-review \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --llm-output llm_results.json \
    --bulk 2
```

The LLM review sends each student's three phases of code plus the
writeup to Gemini and evaluates against the rubric items covering:

- Code structure (compiles, correct files)
- Phase 2 architecture (timer interrupt, sleep mode)
- Phase 3 architecture (state machine PWM)
- Documentation quality (power estimates, measurements, comparisons)

`--bulk N` runs the review N times with shuffled student order and
keeps the most-consistent verdict, smoothing out per-call variance.

### Step 7 — Combined grading (Steps 5 + 6 in one shot)

```bash
python -m grading.lab2.grade --grade-batch \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --video-dir ./videos \
    --calibration ./calibration.json \
    --video-output video_results.json \
    --llm-output llm_results.json \
    --bulk 2
```

Use `--skip-video` to run only the LLM review, or `--skip-llm` to run
only the video analysis.

### Step 8 — Generate grades and reports

#### 8a. Export and edit the rubric

```bash
python -m grading.lab2.score_results --export-rubric rubric.yaml
```

Edit the `points` values in the YAML to match your desired weighting.

#### 8b. Score results

```bash
python -m grading.lab2.score_results \
    --video-results video_results.json \
    --llm-results llm_results.json \
    --rubric rubric.yaml \
    --grades-csv grades.csv \
    --reports-dir reports/
```

This produces:
- `grades.csv` — one row per student, with per-phase video scores,
  per-rubric-item LLM verdicts, and a `grand_total` column
- `reports/<student>.txt` — a human-readable per-student report
  combining video and LLM findings, suitable for posting as a Canvas
  comment

### Step 9 — Upload grades to Canvas

Each phase has its own Canvas assignment, but the writeup-related
rubric items only live in Phase 3.  A typical pattern is to post the
full rubric report to Phase 3 with the grand total, and post each
phase's compile/flash status to its own assignment as a smaller note:

```bash
python -m grading.canvas \
    --csv grades.csv \
    --assignment-id 510247 \
    --student-column student \
    --score-column "grand_total (max N)" \
    --comment-column rubric_text
```

Replace `N` with whatever max you computed in Step 8b, and the
assignment id with whichever phase you're targeting.

## Rubric Items

### Video Analysis

#### Phase 1 and Phase 2 (same as Lab 1)

| Item                        | Description                                 |
| --------------------------- | ------------------------------------------- |
| `distinct_rings`            | Both LED rings active                       |
| `all_24_leds_seen`          | All 24 LEDs activated                       |
| `timing_1hz`                | Timing ~1 Hz                                |
| `inner_clockwise_sequence`  | Inner ring steps clockwise                  |
| `outer_clockwise_sequence`  | Outer ring steps clockwise                  |
| `inner_sequence_wrap`       | Inner ring wraps (11→0)                     |
| `outer_sequence_wrap`       | Outer ring wraps (11→0)                     |
| `full_clock_cycle`          | Complete 12-hour clock cycle                |
| `hour_increment_at_wrap`    | Hour advances on second wrap                |

#### Phase 3 (Lab 1 items + PWM)

All Phase 1/2 items above, plus:

| Item                   | Description                                       |
| ---------------------- | ------------------------------------------------- |
| `pwm_detected`         | PWM modulation detected (via brightness or CV)    |
| `reduced_brightness`   | LED brightness clearly reduced (duty < 100%)      |
| `no_visible_flicker`   | PWM frequency above human flicker fusion (~50 Hz) |

### Code Review (LLM, all phases + writeup)

| Item                              | Description                                       |
| --------------------------------- | ------------------------------------------------- |
| `phase1_compiles`                 | Phase 1 code compiles                             |
| `phase1_baseline_documented`      | Baseline power estimated/measured and documented  |
| `phase2_compiles`                 | Phase 2 code compiles                             |
| `phase2_timer_interrupt`          | Uses timer interrupt (not busy-wait)              |
| `phase2_sleep_mode`               | Enters standby/sleep between ticks                |
| `phase2_sleep_power_documented`   | Sleep-mode power documented                       |
| `phase2_power_reduction_explained`| Explains why sleep reduces power                  |
| `phase3_compiles`                 | Phase 3 code compiles                             |
| `phase3_state_machine_pwm`        | PWM implemented via state machine                 |
| `phase3_state_machine_documented` | State machine explained in writeup                |
| `phase3_pwm_frequency_justified`  | PWM frequency justified (flicker vs power)        |
| `phase3_pwm_power_documented`     | PWM-phase power documented                        |
| `phase3_cross_phase_comparison`   | Power compared across all three phases            |

## Troubleshooting

### "flash FAIL" with no explanation

The capture CSV's `flash_errors` column has the first lines of DSLite's
stderr/stdout for every failed row.  If `flash_errors` is also empty,
DSLite produced no output at all (rare; usually means the XDS110 is
hung).  Try unplugging and replugging the board.

### Video records at 5 fps instead of 30 fps

On Linux, this usually means ffmpeg defaulted to YUYV pixel format,
which exceeds USB 2.0 bandwidth at HD resolution and gets clamped to
~5 fps by the v4l2 driver.  The grader sets `-input_format mjpeg` by
default to avoid this; if your camera doesn't support MJPEG, override
with `DALI_FFMPEG_INPUT_FORMAT=yuyv422` and `--video-size 640x480`.

On macOS, this usually means avfoundation negotiated a frame rate the
camera doesn't actually support and fell back to a slow auto-exposure
mode.  Force a known-good resolution with `--video-size 640x480`.

You can list supported avfoundation modes by deliberately requesting
an impossible frame rate; ffmpeg will dump the full mode list:

```bash
ffmpeg -f avfoundation -framerate 1 -i "0" -t 0.1 -y /tmp/test.mp4
```

### Phase 3 PWM analysis says everything failed

Check the recorded fps with `ffprobe`.  If it's well below 30 fps, the
brightness-reduction analyzer can't tell working PWM from broken PWM.
Re-run capture with `--video-size 640x480` and verify the actual fps.

### Compile passes but the .out file is missing

If `compile_success` is True in `capture_results.csv` but
`flash_success` is False with no error in `flash_errors`, look in
`build_dir` (also in the CSV) — if there's no `.out` file there, the
linker actually produced no binary, which usually means a missing
infrastructure file.  Check `template_files/lab2-{1,2,3}/`.

### A student's submission is missing a phase

This is handled gracefully.  Missing phases are logged as
`no submission` during capture, scored as `NO_DATA` (0 points) during
analysis, and reported as missing in the per-student rubric report.

## File Reference

### Assessment primitives (`assess/`)

| File                         | Purpose                                          |
| ---------------------------- | ------------------------------------------------ |
| `assess/build.py`            | Extract, compile, flash, record (shared)         |
| `assess/video.py`            | OpenCV LED detection from video frames           |
| `assess/lab1_score.py`       | Lab 1 clock-behavior scoring (reused by Lab 2)   |
| `assess/lab2_score.py`       | Lab 2 scoring: Phase 1/2 + Phase 3 PWM analysis  |
| `assess/lab2_code_review.py` | Lab 2 LLM rubric and single-student review       |

### Grading workflows (`grading/lab2/`)

| File                              | Purpose                                     |
| --------------------------------- | ------------------------------------------- |
| `grading/lab2/grade.py`           | Main orchestrator (capture, analyze, review)|
| `grading/lab2/code_review.py`     | Bulk review wrapper                         |
| `grading/lab2/score_results.py`   | Combine results, apply rubric, gen reports  |
| `grading/lab2/GRADING.md`         | This file                                   |
