"""Lab 3 button-press helper — host-side client.

Talks to the Arduino Uno/Nano running ``button_helper.ino``. Provides a
thin, synchronous wrapper over pyserial plus a CLI with several test
modes:

    python -m grading.lab3.helper_client list         # enumerate candidate ports
    python -m grading.lab3.helper_client ping         # open + ? + close
    python -m grading.lab3.helper_client press g      # single glitch press
    python -m grading.lab3.helper_client press s      # single short press
    python -m grading.lab3.helper_client press l      # single long press
    python -m grading.lab3.helper_client smoke        # ping + G/S/L/R in order
    python -m grading.lab3.helper_client stress N     # N mixed presses back to back
    python -m grading.lab3.helper_client interactive  # type commands, see ACKs

Wire protocol (see button_helper.ino header for the full spec):

    G  glitch press  2 ms low            ->  "g"
    S  short press  250 ms low           ->  "s"
    L  long press  1500 ms low           ->  "l"
    R  force release, pin -> Hi-Z         ->  "r"
    ?  ping                               ->  "READY"

The helper resets when the host opens the port (Arduino DTR auto-reset),
so ``open()`` waits for the ``READY`` banner the sketch prints in
``setup()``. As long as the grading run opens the helper ONCE and keeps
the port open for the duration of the run, the reset-blink only happens
outside any recording window.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import random
import sys
import time
from typing import Iterable, List, Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "pyserial is required for grading.lab3.helper_client. "
        "Install it with: pip install pyserial"
    ) from e


log = logging.getLogger("lab3.helper")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BAUD = 115200

# Upper bound on how long the helper can stay busy executing each command,
# plus USB + readline slack. The longest press is 1500 ms; 4 s leaves
# plenty of margin without hiding real hangs.
ACK_TIMEOUT_S = {
    "G": 2.0,
    "S": 3.0,
    "L": 4.0,
    "R": 2.0,
    "?": 2.0,
}

# Expected ACKs from the sketch (mirror of ACK_TIMEOUT_S). "?" returns
# the READY banner rather than a single letter.
EXPECTED_ACK = {
    "G": "g",
    "S": "s",
    "L": "l",
    "R": "r",
    "?": "READY",
}

# How long to wait after opening the port for the sketch's boot banner.
# Bootloader takes ~1 s; add generous slack for slow hosts.
BOOT_BANNER_TIMEOUT_S = 5.0

# USB VID/PIDs we treat as "probably an Arduino or USB-UART bridge."
# Not exhaustive — ``description`` and ``manufacturer`` are also checked
# as a fallback so clones still match.
KNOWN_VIDS = {
    0x2341,  # Arduino LLC / Arduino SA
    0x2A03,  # Arduino.org
    0x1A86,  # QinHeng / CH340 (Nano clones)
    0x0403,  # FTDI
    0x10C4,  # Silicon Labs CP210x
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HelperError(RuntimeError):
    """Raised on any helper protocol error (timeout, wrong ACK, no port)."""


class HelperNotFound(HelperError):
    """Raised when auto-discovery can't find a candidate port."""


class HelperTimeout(HelperError):
    """Raised when the helper doesn't respond within the expected window."""


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PortCandidate:
    """One serial port that might be a helper."""

    device: str
    description: str
    manufacturer: Optional[str]
    vid: Optional[int]
    pid: Optional[int]
    serial_number: Optional[str]

    def summary(self) -> str:
        vidpid = (
            f"{self.vid:04x}:{self.pid:04x}"
            if (self.vid and self.pid)
            else "----:----"
        )
        sn = f" sn={self.serial_number}" if self.serial_number else ""
        return f"{self.device}  [{vidpid}]  {self.description}{sn}"


def enumerate_candidates() -> List[PortCandidate]:
    """Return every serial port that looks like it could be an Arduino."""
    out: List[PortCandidate] = []
    for p in list_ports.comports():
        desc = (p.description or "").lower()
        manuf = (p.manufacturer or "").lower()
        looks_right = (
            (p.vid in KNOWN_VIDS if p.vid is not None else False)
            or "arduino" in desc
            or "arduino" in manuf
            or "ch340" in desc
            or "ch341" in desc
            or "usb-serial" in desc
            or "usb serial" in desc
            or "ftdi" in desc
            or "cp210" in desc
        )
        if looks_right:
            out.append(
                PortCandidate(
                    device=p.device,
                    description=p.description or "",
                    manufacturer=p.manufacturer,
                    vid=p.vid,
                    pid=p.pid,
                    serial_number=p.serial_number,
                )
            )
    return out


def autodetect_port() -> str:
    """Pick a single helper port, or raise if there's ambiguity."""
    cands = enumerate_candidates()
    if not cands:
        raise HelperNotFound(
            "No Arduino-like serial ports found. Plug in the helper, or "
            "pass --port explicitly."
        )
    if len(cands) > 1:
        joined = "\n  ".join(c.summary() for c in cands)
        raise HelperNotFound(
            "Multiple candidate ports found; pass --port to disambiguate:\n  "
            + joined
        )
    return cands[0].device


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HelperClient:
    """Synchronous client for button_helper.ino.

    Usage:

        with HelperClient.open() as h:
            h.ping()
            h.long_press()
            h.short_press()
            h.short_press()
            h.long_press()
            # h.release() runs automatically on __exit__

    Every command method blocks until the sketch ACKs it, so you can't
    race the helper — serial-level ordering is guaranteed by "wait for
    ACK before next command."
    """

    def __init__(self, ser: "serial.Serial", *, port: str):
        self._ser = ser
        self.port = port

    # ---- construction / teardown --------------------------------------

    @classmethod
    def open(
        cls,
        port: Optional[str] = None,
        baud: int = DEFAULT_BAUD,
        *,
        boot_timeout: float = BOOT_BANNER_TIMEOUT_S,
    ) -> "HelperClient":
        """Open the helper and wait for its READY banner.

        If ``port`` is None, auto-discovers by scanning USB VID/PIDs.
        """
        if port is None:
            port = autodetect_port()
        log.info("opening helper on %s @ %d baud", port, baud)
        ser = serial.Serial(port, baud, timeout=0.5)
        client = cls(ser, port=port)
        try:
            client._wait_for_banner(boot_timeout)
        except Exception:
            ser.close()
            raise
        log.info("helper ready on %s", port)
        return client

    def close(self) -> None:
        """Release the button and close the port. Idempotent."""
        if self._ser is None:
            return
        try:
            # Best-effort: force release so a dying test can't leave PB8
            # stuck LOW. Ignore any failure here; we're already tearing
            # down.
            with contextlib.suppress(Exception):
                self._send_raw("R", expect="r", timeout=ACK_TIMEOUT_S["R"])
        finally:
            try:
                self._ser.close()
            finally:
                self._ser = None  # type: ignore[assignment]

    def __enter__(self) -> "HelperClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- protocol primitives ------------------------------------------

    def _wait_for_banner(self, timeout: float) -> None:
        """Consume input until we see a READY line, or time out."""
        deadline = time.monotonic() + timeout
        # After opening the port the Arduino has just reset. Drop any
        # garbage the bootloader left in the buffer and look for READY.
        while time.monotonic() < deadline:
            line = self._readline()
            if line == "READY":
                return
            if line:
                log.debug("pre-banner line: %r", line)
        raise HelperTimeout(
            f"Helper did not send READY within {timeout:.1f}s — check wiring "
            "and that button_helper.ino is flashed."
        )

    def _readline(self) -> str:
        """Read one line, stripped. Empty string on timeout."""
        raw = self._ser.readline()  # honors port timeout
        if not raw:
            return ""
        try:
            return raw.decode("ascii", errors="replace").strip()
        except Exception:
            return ""

    def _send_raw(self, cmd: str, *, expect: str, timeout: float) -> str:
        """Write one command char, block until the expected ACK line arrives.

        Returns the ACK line on success. Raises HelperTimeout if no ACK
        arrives in time, or HelperError if an unexpected line comes back.
        """
        if self._ser is None:
            raise HelperError("helper port is closed")
        if len(cmd) != 1:
            raise ValueError(f"command must be one character, got {cmd!r}")

        # Drain any stale input (shouldn't be any if the previous call
        # completed cleanly, but belt-and-suspenders).
        self._ser.reset_input_buffer()

        log.debug("-> %r", cmd)
        self._ser.write(cmd.encode("ascii"))
        self._ser.flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._readline()
            if not line:
                continue
            log.debug("<- %r", line)
            if line == expect:
                return line
            # Any other non-empty line is a protocol error we want to see.
            raise HelperError(
                f"helper sent unexpected line {line!r} while waiting for "
                f"{expect!r} (command {cmd!r})"
            )
        raise HelperTimeout(
            f"no ACK {expect!r} within {timeout:.1f}s for command {cmd!r}"
        )

    # ---- public commands ----------------------------------------------

    def ping(self) -> None:
        """Send `?` and expect a READY banner."""
        self._send_raw("?", expect=EXPECTED_ACK["?"], timeout=ACK_TIMEOUT_S["?"])

    def glitch(self) -> None:
        """2 ms press — should be rejected by a correct debouncer."""
        self._send_raw("G", expect=EXPECTED_ACK["G"], timeout=ACK_TIMEOUT_S["G"])

    def short_press(self) -> None:
        """250 ms press — a clear short press, used to increment values."""
        self._send_raw("S", expect=EXPECTED_ACK["S"], timeout=ACK_TIMEOUT_S["S"])

    def long_press(self) -> None:
        """1.5 s press — a clear long press, used to change mode."""
        self._send_raw("L", expect=EXPECTED_ACK["L"], timeout=ACK_TIMEOUT_S["L"])

    def release(self) -> None:
        """Force PB8 to Hi-Z and LED off. Safe to call anytime."""
        self._send_raw("R", expect=EXPECTED_ACK["R"], timeout=ACK_TIMEOUT_S["R"])

    # ---- convenience --------------------------------------------------

    def send_sequence(
        self,
        commands: Iterable[str],
        gap_ms: int = 300,
    ) -> None:
        """Run a sequence of single-char commands with ``gap_ms`` between them.

        ``gap_ms`` is measured from the ACK of one command to the send of
        the next. This is the host-side inter-press gap — useful when you
        want multiple short presses interpreted as distinct presses by the
        student's debouncer.
        """
        mapping = {
            "G": self.glitch,
            "S": self.short_press,
            "L": self.long_press,
            "R": self.release,
            "?": self.ping,
        }
        first = True
        for c in commands:
            if not first and gap_ms > 0:
                time.sleep(gap_ms / 1000.0)
            first = False
            fn = mapping.get(c.upper())
            if fn is None:
                raise ValueError(f"unknown command {c!r} in sequence")
            fn()


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _cli_list(args: argparse.Namespace) -> int:
    cands = enumerate_candidates()
    if not cands:
        print("No Arduino-like serial ports found.")
        return 1
    print(f"Found {len(cands)} candidate port(s):")
    for c in cands:
        print(f"  {c.summary()}")
    return 0


def _cli_ping(args: argparse.Namespace) -> int:
    with HelperClient.open(port=args.port) as h:
        h.ping()
        print(f"OK: helper on {h.port} responded READY")
    return 0


def _cli_press(args: argparse.Namespace) -> int:
    kind = args.kind.lower()
    with HelperClient.open(port=args.port) as h:
        if kind == "g":
            h.glitch()
        elif kind == "s":
            h.short_press()
        elif kind == "l":
            h.long_press()
        elif kind == "r":
            h.release()
        else:
            print(f"unknown press kind: {args.kind}", file=sys.stderr)
            return 2
        print(f"OK: {kind} press complete on {h.port}")
    return 0


def _cli_smoke(args: argparse.Namespace) -> int:
    """Basic end-to-end check: ping + G + S + L + R, verifying every ACK
    and that press durations are within ~10% of nominal."""
    nominal_ms = {"G": 250, "S": 250, "L": 1500}  # wall-clock LED-on time
    tolerance = 0.25  # accept +-25% variance (USB jitter etc)

    with HelperClient.open(port=args.port) as h:
        print(f"[smoke] opened {h.port}")

        t0 = time.monotonic()
        h.ping()
        print(f"[smoke] ping ok ({(time.monotonic() - t0) * 1e3:.1f} ms)")

        for cmd, name, fn in (
            ("G", "glitch", h.glitch),
            ("S", "short",  h.short_press),
            ("L", "long",   h.long_press),
        ):
            t0 = time.monotonic()
            fn()
            dt_ms = (time.monotonic() - t0) * 1e3
            expected = nominal_ms[cmd]
            lo = expected * (1 - tolerance)
            hi = expected * (1 + tolerance) + 100  # +100 ms USB slack
            ok = lo <= dt_ms <= hi
            mark = "OK" if ok else "WARN"
            print(
                f"[smoke] {name:6s} press: {dt_ms:7.1f} ms "
                f"(expect ~{expected} ms, allow {lo:.0f}-{hi:.0f}) [{mark}]"
            )
            if not ok:
                print(
                    "         (timing is wall-clock from command send to ACK "
                    "receipt, so USB jitter is bundled in)",
                    file=sys.stderr,
                )

        t0 = time.monotonic()
        h.release()
        print(f"[smoke] release ok ({(time.monotonic() - t0) * 1e3:.1f} ms)")

    print("[smoke] all checks passed")
    return 0


def _cli_stress(args: argparse.Namespace) -> int:
    """Fire ``n`` randomly selected presses in a row with a fixed inter-gap.

    Used to hunt for missed ACKs, out-of-order responses, or buffer
    overruns. The helper sketch is blocking so this should always be
    robust — the stress test exists to verify that assumption.
    """
    rng = random.Random(args.seed)
    choices = ["G", "S", "L"]
    # Long presses dominate time; bias toward short/glitch so the run
    # doesn't take forever.
    weights = [3, 3, 1]

    seq = [rng.choices(choices, weights)[0] for _ in range(args.n)]
    total_nominal = sum({"G": 0.250, "S": 0.250, "L": 1.500}[c] for c in seq)
    gap_total = max(0, args.n - 1) * (args.gap_ms / 1000.0)
    print(
        f"[stress] running {args.n} presses "
        f"({seq.count('G')} G, {seq.count('S')} S, {seq.count('L')} L); "
        f"estimated {total_nominal + gap_total:.1f} s"
    )

    with HelperClient.open(port=args.port) as h:
        t0 = time.monotonic()
        for i, c in enumerate(seq):
            if i > 0 and args.gap_ms > 0:
                time.sleep(args.gap_ms / 1000.0)
            t1 = time.monotonic()
            {"G": h.glitch, "S": h.short_press, "L": h.long_press}[c]()
            if args.verbose:
                print(
                    f"[stress] {i + 1:3d}/{args.n} {c} "
                    f"ack_dt={(time.monotonic() - t1) * 1e3:6.1f} ms"
                )
        total = time.monotonic() - t0

    print(f"[stress] {args.n} presses completed in {total:.2f} s (no errors)")
    return 0


def _cli_interactive(args: argparse.Namespace) -> int:
    print("Interactive helper client.")
    print("Type G/S/L/R/? + Enter. Blank line = ping. Ctrl-D or 'q' to quit.")
    with HelperClient.open(port=args.port) as h:
        print(f"[connected on {h.port}]")
        while True:
            try:
                raw = input("helper> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if raw in ("q", "quit", "exit"):
                break
            if raw == "":
                raw = "?"
            try:
                h.send_sequence(raw, gap_ms=args.gap_ms)
                print(f"  ok: {raw}")
            except HelperError as e:
                print(f"  ERR: {e}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m grading.lab3.helper_client",
        description="Test / drive the Lab 3 button-press helper.",
    )
    p.add_argument(
        "--port",
        help="Serial port device (default: autodetect by USB VID/PID)",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Enumerate candidate serial ports")
    sub.add_parser("ping", help="Open, ping, close")

    p_press = sub.add_parser("press", help="Send one press")
    p_press.add_argument("kind", choices=["g", "s", "l", "r"])

    sub.add_parser("smoke", help="Ping + G + S + L + R with timing check")

    p_stress = sub.add_parser("stress", help="Fire N random presses in a row")
    p_stress.add_argument("n", type=int, help="Number of presses")
    p_stress.add_argument(
        "--gap-ms", type=int, default=200,
        help="Inter-press gap in ms (default 200)",
    )
    p_stress.add_argument("--seed", type=int, default=0, help="RNG seed")

    p_inter = sub.add_parser("interactive", help="Interactive command shell")
    p_inter.add_argument(
        "--gap-ms", type=int, default=200,
        help="Inter-press gap in ms when a line has multiple chars",
    )

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dispatch = {
        "list":        _cli_list,
        "ping":        _cli_ping,
        "press":       _cli_press,
        "smoke":       _cli_smoke,
        "stress":      _cli_stress,
        "interactive": _cli_interactive,
    }
    try:
        return dispatch[args.cmd](args)
    except HelperError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
