# Lab 3 Grading Guide

Lab 3 (Button-Controlled Time Setting) grading uses a multi-segment
**video capture** pipeline combined with **LLM code review**. Unlike
Labs 1 and 2 where each video captures a single firmware phase, Lab 3
records one continuous video per student while reflashing the board
between scripted button-press sequences. Each reflash+stimulus block
is called a *segment* and is scored independently.

## Overview

| Component         | Purpose                                         |
|-------------------|-------------------------------------------------|
| Arduino helper    | Drives button presses on PB8 via UART commands  |
| Capture pipeline  | Compile once, reflash N times, record one video  |
| Video analysis    | Slice video at debug-LED transitions, score each segment |
| LLM code review   | Evaluate FSM structure, debounce logic, writeup  |
| Score + reports   | Combine video + LLM, apply rubric, generate CSV  |

```
 Student zip
      |
      v
 +-----------+
 | compile   |  once per student
 +-----------+
      |
      v
 [start ffmpeg recording]
      |
      v
 +---------------------------------+
 | for each segment (12 total):    |
 |   flash student .out            |
 |   wait boot + warmup            |
 |   run stimulus (Arduino helper) |
 |   observe window                |
 +---------------------------------+
      |
      v
 [SIGINT ffmpeg]
      |
      v
 <student>.mp4 + <student>.json
      |                |
      v                v
 video_results.json   llm_results.json
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
  grades.csv     reports/*.txt
```

## Prerequisites

- Python venv with DALI requirements (`pip install -r requirements.txt`)
- TI ARM Clang compiler and MSPM0 SDK
- DSLite on PATH or via `DSLITE_PATH`
- ffmpeg installed
- Camera at 30 fps, 640x480
- Arduino Uno/Nano flashed with `button_helper.ino` (see below)
- Gemini API key in `GEMINI_API_KEY` (for LLM review only)

## Hardware Setup

### Arduino Helper

The Arduino helper generates timed button presses on the student
board's PB8 input. It runs `grading/lab3/button_helper/button_helper.ino`.

**Wiring:**
- Arduino D2 -> student PB8 (open-drain: drives LOW for press, Hi-Z for release)
- Arduino D13 -> sync LED in camera frame (mirrors press timing)
- Arduino GND -> student GND

**Flash once before grading:**
```bash
# Using Arduino IDE or arduino-cli:
arduino-cli compile --fqbn arduino:avr:uno grading/lab3/button_helper/
arduino-cli upload --fqbn arduino:avr:uno -p /dev/ttyACM0 grading/lab3/button_helper/
```

**Verify:**
```bash
python -m grading.lab3.helper_client smoke
```

### Camera Framing

The camera must see all three groups of LEDs simultaneously:
- **24 clock LEDs** (hour and minute rings on student PCB)
- **Debug LED** (XDS110 programming indicator — flashes during each reflash)
- **Sync LED** (Arduino D13 — flashes during each button press)

Do not move the camera between students. All videos must share the
same calibration.

## Step-by-Step Workflow

### Step 0 — Lay out submissions

```
~/lab3_grading/
  submissions/     # student .zip files from Canvas
```

### Step 1 — Set up environment

```bash
cd ~/lab3_grading
source ~/dali-venv/bin/activate
export DALI_ROOT=/path/to/DALI
export PYTHONPATH="$DALI_ROOT"
set -a; source "$DALI_ROOT/.env"; set +a
```

### Step 2 — Verify helper

```bash
python -m grading.lab3.helper_client smoke
```

### Step 3 — Capture videos

```bash
python -m grading.lab3.grade --capture \
    --submissions ./submissions \
    --ccxml "$DALI_ROOT/MSPM0G3507.ccxml" \
    --video-dir ./videos \
    --keep-builds ./builds \
    --results-csv capture_results.csv
```

Each student produces `videos/<student>.mp4` (continuous recording,
all 12 segments) and `videos/<student>.json` (host-side timing log).

Quick test against one student:
```bash
python -m grading.lab3.grade --capture \
    --submissions ./submissions \
    --only reference_solution.zip \
    --video-dir ./test_videos \
    --quick
```

`--quick` runs only 3 segments (baseline, debounce, enter_hour_set)
for fast iteration (~1 min).

**Timing:** ~4.5 min per student for all 12 segments.

### Step 4 — Calibrate

Calibrate LED positions using a video where the clock is running:

```bash
python -m grading.calibrate \
    --video ./videos/<good_student>.mp4 \
    --output calibration.json
```

Mark the debug LED, 12 outer-ring LEDs (clockwise from 12 o'clock),
and 12 inner-ring LEDs.

> **Note:** The sync LED (Arduino D13) calibration target is pending
> ([#11](https://github.com/ckemere/DALI/issues/11)). For now, the
> analyzer uses the host-side timing log from `<student>.json` for
> press-event timing instead of video-detected sync LED edges.

### Step 5 — Analyze videos

*TODO: analyzer not yet implemented. Next step after rubric.*

```bash
python -m grading.lab3.grade --analyze-videos \
    --video-dir ./videos \
    --calibration calibration.json \
    --video-output video_results.json
```

### Step 6 — Run LLM code review

*TODO: LLM review module not yet implemented.*

```bash
python -m grading.lab3.grade --code-review \
    --submissions ./submissions \
    --llm-output llm_results.json
```

### Step 7 — Export and edit rubric

```bash
python -m grading.lab3.score_results --export-rubric rubric.yaml
```

Edit point values in `rubric.yaml` to taste.

### Step 8 — Generate grades

```bash
python -m grading.lab3.score_results \
    --video-results video_results.json \
    --llm-results llm_results.json \
    --rubric rubric.yaml \
    --grades-csv grades.csv \
    --reports-dir reports/
```

## Test Segments

12 segments per student, each starting from a fresh reflash:

| #  | Name                  | Stimulus          | What it tests                          |
|----|-----------------------|-------------------|----------------------------------------|
|  1 | baseline              | (none)            | Normal clock runs, ~1 Hz timing        |
|  2 | debounce_reject       | 1 glitch (2 ms)   | Glitch press rejected                  |
|  3 | enter_hour_set        | 1 long            | Hour LED flashes, minute steady        |
|  4 | hour_increment_3      | 1 long + 3 short  | Hour advances by 3                     |
|  5 | hour_wrap             | 1 long + 13 short | Hour wraps 12 -> 1                     |
|  6 | enter_minute_set      | 2 long            | Minute LED flashes, hour steady        |
|  7 | minute_increment_4    | 2 long + 4 short  | Minute advances by 4 LEDs              |
|  8 | minute_wrap           | 2 long + 13 short | Minute wraps 55 -> 0                   |
|  9 | return_to_normal      | 3 long            | Clock resumes (non-EC students)        |
| 10 | enter_brightness_set  | 3 long            | Both LEDs flash (EC students)          |
| 11 | brightness_increment  | 3 long + 5 short  | Brightness changes (EC)                |
| 12 | return_to_normal_ec   | 4 long            | Clock resumes (EC students)            |

Segments 10-12 are for brightness extra-credit. For non-EC students,
these segments produce non-matching results that the scorer ignores
(the student already passed via segment 9).

## Rubric Items

### Video Analysis (37 base + 10 EC points)

| Item | Description | Points |
|------|-------------|--------|
| `normal_clock_runs` | Clock runs in Normal mode | 2 |
| `normal_clock_timing_1hz` | Timing ~1 Hz | 2 |
| `debounce_rejects_glitch` | 2 ms glitch rejected | 2 |
| `long_enters_hour_set` | Long press enters Hour-Set | 3 |
| `hour_flashes_in_hour_set` | Hour LED flashes | 3 |
| `minute_steady_in_hour_set` | Minute LED steady | 1 |
| `clock_does_not_advance_in_hour_set` | Time frozen in Hour-Set | 1 |
| `short_increments_hour` | Short presses advance hour | 3 |
| `hour_wraps_12_to_1` | Hour wraps 12 -> 1 | 2 |
| `long_enters_minute_set` | Second long press enters Minute-Set | 3 |
| `minute_flashes_in_minute_set` | Minute LED flashes | 3 |
| `hour_steady_in_minute_set` | Hour LED steady | 1 |
| `clock_does_not_advance_in_minute_set` | Time frozen in Minute-Set | 1 |
| `short_increments_minute` | Short presses advance minute | 3 |
| `minute_wraps_55_to_0` | Minute wraps 55 -> 0 | 2 |
| `long_returns_to_normal` | Long press(es) return to Normal | 3 |
| `clock_advances_after_return` | Clock resumes after return | 2 |
| **EC:** `long_enters_brightness_set` | 3 longs enter Brightness-Set | 2 |
| **EC:** `both_flash_in_brightness_set` | Both LEDs flash | 2 |
| **EC:** `brightness_responds_to_short` | Brightness changes on press | 2 |
| **EC:** `long_returns_to_normal_ec` | 4 longs return to Normal | 2 |
| **EC:** `clock_advances_after_return_ec` | Clock resumes (EC path) | 2 |

### Code Review — LLM (23 base + 6 EC points)

| Item | Description | Points |
|------|-------------|--------|
| `compiles` | Code compiles | 2 |
| `button_gpio_init` | PB8 configured as input | 1 |
| `fsm_structure` | State machine identifiable | 3 |
| `debounce_logic` | Debounce >= 5 ms | 2 |
| `long_short_detection` | Long vs short threshold ~1 s | 2 |
| `mode_transitions` | Correct mode transition sequence | 2 |
| `hour_increment_logic` | Hour increment + wrap | 1 |
| `minute_increment_logic` | Minute increment by 5 min | 1 |
| `wrap_logic` | Both hour and minute wrap | 1 |
| `flash_via_fsm` | Flashing via FSM, not delay | 2 |
| `time_freeze_in_set_mode` | Time frozen in set modes | 1 |
| `time_resume_on_exit` | New time active on exit | 1 |
| `readme_fsm_description` | README describes FSM | 2 |
| `readme_press_detection` | README explains press detection | 2 |
| **EC:** `brightness_mode_implemented` | Brightness mode in code | 2 |
| **EC:** `brightness_levels_ge_15` | >= 15 brightness levels | 2 |
| **EC:** `brightness_wrap_to_min` | Brightness wraps max -> min | 2 |

### Scoring alternatives

`long_returns_to_normal` and `clock_advances_after_return` can be
satisfied by **either** segment 9 (3 longs, for non-EC students) or
segment 12 (4 longs, for EC students). The scorer checks both and
grants points if either passes.

## File Reference

### Helper firmware and client

| File | Purpose |
|------|---------|
| `grading/lab3/button_helper/button_helper.ino` | Arduino stimulus helper |
| `grading/lab3/helper_client.py` | Python serial client + CLI |

### Grading pipeline

| File | Purpose |
|------|---------|
| `grading/lab3/segments.py` | Segment definitions + stimulus DSL |
| `grading/lab3/grade.py` | Capture orchestrator (--capture) |
| `grading/lab3/score_results.py` | Rubric export, scoring, reports |
| `assess/lab3_score.py` | Rubric items, points, descriptions |
| `grading/lab3/GRADING.md` | This file |
