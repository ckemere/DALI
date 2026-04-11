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
 | record    |   | record    |   | record    | (high FPS)
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
       | canvas_upload  |  --> Canvas LMS
       +---------------+
```

## Prerequisites

Same as Lab 1 grading (see `grading/lab1/GRADING.md`), plus:

- Camera capable of **120+ fps** recording (for Phase 3 PWM flicker detection)
- Your camera must support the requested frame rate via v4l2 (Linux) or
  avfoundation (macOS)

## Step-by-Step Workflow

### Step 0: Download Submissions

Download the submission zip files from Canvas into three separate directories:

```
lab2_grading/
  phase1_submissions/     # zips from Canvas assignment 510245
  phase2_submissions/     # zips from Canvas assignment 510246
  phase3_submissions/     # zips from Canvas assignment 510247
```

### Step 1: Calibrate the Camera

Use the same calibration process as Lab 1:

```bash
python -m grading.calibrate --camera 0 --output calibration.json
```

### Step 2: Compile, Flash, and Record Videos

```bash
python -m grading.lab2.grade --capture \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --ccxml MSPM0G3507.ccxml \
    --calibration calibration.json \
    --video-dir ./videos \
    --results-csv capture_results.csv
```

This processes each student's three phases sequentially:
1. Extracts the zip
2. Copies infrastructure files from `template_files/lab2-{1,2,3}/`
3. Compiles with TI ARM Clang
4. Flashes to the board via DSLite
5. Records video (30 fps for Phase 1/2, **120 fps for Phase 3**)

Videos are saved to `videos/phase1/`, `videos/phase2/`, `videos/phase3/`.

**Compile-only mode** (no hardware):

```bash
python -m grading.lab2.grade --capture \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --compile-only \
    --results-csv compile_results.csv
```

**Custom Phase 3 recording settings:**

```bash
python -m grading.lab2.grade --capture \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --phase3-fps 240 \
    --phase3-duration 60 \
    ...
```

**Keeping build artifacts for debugging:**

By default each student/phase compiles into a `tempfile.mkdtemp` that is
deleted as soon as the phase is recorded.  Pass `--keep-builds DIR` to
keep the `.out` (and `.map`, intermediate `.o`) files around so you can
re-flash a failing student by hand:

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

Each build is then at `./builds/<student>/<phase>/<lab_output>.out`,
e.g. `./builds/alice/phase3/Lab_2_3.out`.  Re-flash by hand with:

```bash
$DSLITE_PATH load -c "$DALI_ROOT/MSPM0G3507.ccxml" \
    -f ./builds/<student>/phase3/Lab_2_3.out
```

The `capture_results.csv` produced by the run also gains a `build_dir`
column pointing at the directory for each row, plus a `flash_errors`
column with the first lines of DSLite stderr/stdout for any failed
flash (no more silent "flash FAIL").

### Step 3: Analyze Pre-Recorded Videos

If you already have videos recorded (e.g., from a TA session):

```bash
python -m grading.lab2.grade \
    --analyze-videos ./videos \
    --calibration calibration.json \
    --video-output video_results.json
```

Expects `videos/{phase1,phase2,phase3}/<student>.mp4`.

Phase 1 and 2 videos are scored with the standard Lab 1 clock-behavior
rubric (timing, sequence, wrapping, etc.).

Phase 3 videos are analyzed at native frame rate for:
- Standard clock behavior (same checks as Phase 1/2)
- **PWM detection** from brightness fluctuations
- **Duty cycle estimation** (target: ~25%)
- **PWM frequency estimation** via FFT
- **Flicker assessment** (frequency above ~50 Hz = no visible flicker)

### Step 4: Run LLM Code Review

```bash
python -m grading.lab2.grade --code-review \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --llm-output llm_results.json \
    --bulk 2
```

The LLM review sends all three phases' code plus the writeup to Gemini and
evaluates against the rubric items covering:
- Code structure (compiles, correct files)
- Phase 2 architecture (timer interrupt, sleep mode)
- Phase 3 architecture (state machine PWM)
- Documentation quality (power estimates, measurements, comparisons)

### Step 5: Combined Grading (Steps 3 + 4 together)

```bash
python -m grading.lab2.grade --grade-batch \
    --phase1-dir ./phase1_submissions \
    --phase2-dir ./phase2_submissions \
    --phase3-dir ./phase3_submissions \
    --video-dir ./videos \
    --calibration calibration.json \
    --video-output video_results.json \
    --llm-output llm_results.json \
    --bulk 2
```

Use `--skip-video` or `--skip-llm` to run only one phase.

### Step 6: Generate Grades and Reports

#### 6a. Export and edit the rubric

```bash
python -m grading.lab2.score_results --export-rubric rubric.yaml
```

Edit the `points` values in the YAML to match your desired weighting.

#### 6b. Score results

```bash
python -m grading.lab2.score_results \
    --video-results video_results.json \
    --llm-results llm_results.json \
    --rubric rubric.yaml \
    --grades-csv grades.csv \
    --reports-dir reports/
```

### Step 7: Upload Grades to Canvas

Use the generic Canvas uploader:

```bash
python -m grading.canvas \
    --csv grades.csv \
    --assignment-id <ASSIGNMENT_ID> \
    --student-column student \
    --score-column "grand_total (max N)" \
    --comment-column rubric_text
```

## Rubric Items

### Video Analysis (per phase)

#### Phase 1 and Phase 2 (same as Lab 1)

| Item                        | Description                                 |
| --------------------------- | ------------------------------------------- |
| `distinct_rings`            | Both LED rings active                       |
| `all_24_leds_seen`          | All 24 LEDs activated                       |
| `timing_1hz`                | Timing ~1 Hz                                |
| `inner_clockwise_sequence`  | Inner ring steps clockwise                  |
| `outer_clockwise_sequence`  | Outer ring steps clockwise                  |
| `inner_sequence_wrap`       | Inner ring wraps (11->0)                    |
| `outer_sequence_wrap`       | Outer ring wraps (11->0)                    |
| `full_clock_cycle`          | Complete 12-hour clock cycle                |
| `hour_increment_at_wrap`    | Hour advances on second wrap                |

#### Phase 3 (Lab 1 items + PWM)

All Phase 1/2 items above, plus:

| Item                   | Description                                      |
| ---------------------- | ------------------------------------------------ |
| `pwm_detected`         | PWM modulation detected from brightness data     |
| `reduced_brightness`   | LED brightness clearly reduced (duty cycle <100%)|
| `no_visible_flicker`   | PWM frequency high enough (>50 Hz)               |

### Code Review (LLM, all phases + writeup)

| Item                              | Description                                         |
| --------------------------------- | --------------------------------------------------- |
| `phase1_compiles`                 | Phase 1 code compiles                               |
| `phase1_baseline_documented`      | Baseline power estimated/measured and documented     |
| `phase2_compiles`                 | Phase 2 code compiles                               |
| `phase2_timer_interrupt`          | Uses timer interrupt (not busy-wait)                 |
| `phase2_sleep_mode`               | Enters standby/sleep between ticks                   |
| `phase2_sleep_power_documented`   | Sleep-mode power documented                          |
| `phase2_power_reduction_explained`| Explains why sleep reduces power                     |
| `phase3_compiles`                 | Phase 3 code compiles                               |
| `phase3_state_machine_pwm`        | PWM implemented via state machine                    |
| `phase3_state_machine_documented` | State machine explained in writeup                   |
| `phase3_pwm_frequency_justified`  | PWM frequency justified (flicker vs power)           |
| `phase3_pwm_power_documented`     | PWM-phase power documented                           |
| `phase3_cross_phase_comparison`   | Power compared across all three phases               |

## File Reference

### Assessment primitives (`assess/`)

| File                       | Purpose                                          |
| -------------------------- | ------------------------------------------------ |
| `assess/build.py`          | Extract, compile, flash, record (shared)         |
| `assess/video.py`          | OpenCV LED detection from video frames           |
| `assess/lab1_score.py`     | Lab 1 clock-behavior scoring (reused by Lab 2)   |
| `assess/lab2_score.py`     | Lab 2 scoring: Phase 1/2 + Phase 3 PWM analysis  |
| `assess/lab2_code_review.py` | Lab 2 LLM rubric and single-student review     |

### Grading workflows (`grading/lab2/`)

| File                              | Purpose                                    |
| --------------------------------- | ------------------------------------------ |
| `grading/lab2/grade.py`           | Main orchestrator (capture, analyze, review)|
| `grading/lab2/code_review.py`     | Bulk review wrapper                         |
| `grading/lab2/score_results.py`   | Combine results, apply rubric, gen reports  |
| `grading/lab2/GRADING.md`         | This file                                   |
