/*
 * button_helper.ino — Lab 3 grading button-press stimulus helper
 *
 * Runs on an Arduino Uno / Nano. The host grading script drives this
 * helper over USB serial (115200 baud) and issues one-character commands
 * that generate timed pulses on BTN_PIN, which is wired to the student
 * board's PB8 button input. A sync LED in the camera frame flashes for
 * every press so the video analyzer can find press events on the
 * recorded timeline.
 *
 * BTN_PIN is driven as a fake open-drain output: Hi-Z when released
 * (so the student board's pull-up works normally) and actively LOW
 * during a press. We emulate open-drain on AVR by toggling pinMode()
 * between INPUT (Hi-Z) and OUTPUT (driven). The DDR bit is the only
 * thing that changes in the hot path — the PORT bit is pre-loaded to 0
 * at boot.
 *
 * Wiring (Arduino Uno/Nano):
 *   D2  -> student PB8                (open-drain drive)
 *   D13 -> onboard LED + external LED (sync marker in camera frame)
 *   GND -> student GND                (common ground)
 *
 * Protocol (one character in, one lowercase ACK line out):
 *   G    glitch press        2 ms pin-low,    LED held >= 250 ms   -> "g"
 *   S    short press        250 ms pin-low,  LED matches           -> "s"
 *   L    long press        1500 ms pin-low,  LED matches           -> "l"
 *   R    force release      pin -> Hi-Z, LED off                   -> "r"
 *   ?    ping                                                      -> "READY"
 *
 * Whitespace (\r, \n, space) between commands is ignored. Unknown
 * characters produce "?\n" and are otherwise silently dropped. The
 * helper blocks while executing a command (press durations up to 1.5 s);
 * the host must wait for the ACK before sending the next command, which
 * guarantees serial-level ordering without any queue logic on the helper.
 *
 * Timing notes:
 *   - delayMicroseconds() is accurate to ~3 us on 16 MHz AVR, well under
 *     the ~1 ms margin we need for the 2 ms glitch.
 *   - delay() (ms-resolution) is used for >= 250 ms waits. Its accuracy
 *     is easily <1 ms, which is negligible vs. the LED-flash and
 *     press-duration budgets.
 *   - The LED flash envelopes the press by a handful of clock cycles
 *     (two back-to-back register writes); within any video frame that's
 *     simultaneous.
 *   - For the glitch case, the LED is held on for LED_MIN_MS even though
 *     the pin is only LOW for GLITCH_US, so the press is still visible
 *     at 30 fps (~7 frame periods of LED-on).
 */

// ---------- Configuration ---------------------------------------------------

static const uint8_t  BTN_PIN      = 2;     // Digital pin to student PB8
static const uint8_t  LED_PIN      = 13;    // Sync LED (onboard + optional external)

static const uint32_t GLITCH_US    = 2000UL;    // Should be REJECTED by debouncer
static const uint32_t SHORT_MS     = 250UL;     // Clear short press
static const uint32_t LONG_MS      = 1500UL;    // Clear long press (>= 1 s threshold)
static const uint32_t LED_MIN_MS   = 250UL;     // Minimum LED flash for camera visibility

static const uint32_t SERIAL_BAUD  = 115200UL;

// ---------- Pin primitives --------------------------------------------------
//
// The BTN_PIN PORT bit is pre-loaded to 0 at boot and never touched again.
// Flipping DDR is then a single-register op: INPUT = Hi-Z (released),
// OUTPUT = driven LOW (pressed).

static inline void btn_press_low()   { pinMode(BTN_PIN, OUTPUT); }
static inline void btn_release_hiz() { pinMode(BTN_PIN, INPUT);  }

static inline void led_on()  { digitalWrite(LED_PIN, HIGH); }
static inline void led_off() { digitalWrite(LED_PIN, LOW);  }

// ---------- Command implementations -----------------------------------------

// Glitch press: pin LOW 2 ms, LED on for LED_MIN_MS.
// The pin release happens while the LED is still on so the video sees a
// clean rectangular LED pulse rather than a 2 ms blip.
static void cmd_glitch() {
  led_on();
  btn_press_low();
  delayMicroseconds(GLITCH_US);
  btn_release_hiz();
  // LED_MIN_MS total - (GLITCH_US / 1000) already elapsed
  delay(LED_MIN_MS - (GLITCH_US / 1000));
  led_off();
}

// Short press: pin LOW SHORT_MS, LED matches.
static void cmd_short() {
  led_on();
  btn_press_low();
  delay(SHORT_MS);
  btn_release_hiz();
  led_off();
}

// Long press: pin LOW LONG_MS, LED matches.
static void cmd_long() {
  led_on();
  btn_press_low();
  delay(LONG_MS);
  btn_release_hiz();
  led_off();
}

// Panic-release: unconditionally drop pin to Hi-Z and turn off LED.
// Safe to call at any time; used by the host after an error/timeout.
static void cmd_release() {
  btn_release_hiz();
  led_off();
}

// ---------- Arduino entry points --------------------------------------------

void setup() {
  // Pre-load BTN_PIN PORT=0 while still INPUT, so the first flip to
  // OUTPUT later drives LOW without a glitch. pinMode(INPUT) + a digitalWrite
  // while INPUT just sets the PORT bit; the pin stays Hi-Z until DDR flips.
  pinMode(BTN_PIN, INPUT);
  digitalWrite(BTN_PIN, LOW);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.begin(SERIAL_BAUD);
  // Short settle for USB CDC to enumerate on boards that need it.
  // Uno/Nano with real UART ignore this; Leonardo/Micro actually need it.
  while (!Serial && millis() < 2000) { /* wait up to 2 s */ }
  Serial.println(F("READY"));
}

void loop() {
  if (!Serial.available()) return;

  char c = Serial.read();
  switch (c) {
    case 'G': cmd_glitch();  Serial.println(F("g")); break;
    case 'S': cmd_short();   Serial.println(F("s")); break;
    case 'L': cmd_long();    Serial.println(F("l")); break;
    case 'R': cmd_release(); Serial.println(F("r")); break;
    case '?': Serial.println(F("READY"));            break;

    // Ignore whitespace silently — lets the host send "L\n" or "L ".
    case '\r':
    case '\n':
    case ' ':
    case '\t':
      break;

    default:
      // Unknown char: nack so the host notices.
      Serial.println(F("?"));
      break;
  }
}
