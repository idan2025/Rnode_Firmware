#!/usr/bin/env python3
"""rnode_serial.py — KISS/RNode helper for the T1000-E (and friends) fork.

Subcommands:
  show   — read device identity, product/model, current radio settings, EEPROM state
  set    — write one or more radio parameters (frequency/bandwidth/spreading-factor/coding-rate/txpower)
  wipe   — wipe the device EEPROM (destructive; requires --yes or interactive WIPE confirmation)

This module is also importable as a library for tests; the public helper class
is :class:`SerialSession`, which encapsulates the open / detect / read / write
lifecycle around a real or mock serial port.

Hardware notes (T1000-E specific):
  - Radio is Semtech LR1110, not SX126x. The CMD/frame format is the same as
    upstream RNode; only the on-device handler differs.
  - Spreading factor range is 5..12 (LR1110 wider than SX126x's 7..12).
  - Coding rate is the *denominator* 5..8 (the firmware stores denominator-4
    internally, but the on-wire byte is the denominator).
  - TX power range is -17..+22 dBm (LR1110's low-power + high-power PA range).
  - Band 863..928 MHz per the T1000-E datasheet (LR1110 supports 150..960).
  - EEPROM_SIZE is 296 and the ADDR_* map matches upstream RNode's ROM.h, so
    fields parsed by ``rnodeconf.parse_eeprom`` are address-compatible. But
    that caller's checksum mismatch hard-exits via ``graceful_exit()``; we
    parse the dump ourselves (no exit) and fall back to live status reads
    via the per-field "report current value" KISS commands (FW treats 0/0xFF
    arg as a query for frequency/bandwidth/SF/CR/TX-power).

No real serial device is ever touched by this module on its own — the caller
supplies a serial instance (real or mock). Tests under tests/ use a mock.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import struct
import sys
import threading
import time
from typing import Any, Callable, Iterable, Optional

# ---------------------------------------------------------------------------
# Reuse upstream RNode's KISS command set + RNode helper, if installed.
# We don't *require* it (this script can run with a mock serial for tests),
# but every byte we put on the wire matches what RNode.setFrequency/etc.
# would emit, so we guarantee wire-format compatibility.
# ---------------------------------------------------------------------------
try:
    from RNS.Utilities.rnodeconf import KISS as _UPSTREAM_KISS, ROM as _UPSTREAM_ROM
    _HAVE_UPSTREAM = True
except Exception:  # pragma: no cover - exercised only via mocked tests
    _HAVE_UPSTREAM = False
    _UPSTREAM_KISS = None  # type: ignore[assignment]
    _UPSTREAM_ROM = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Local copies of the KISS/ROM constants and EEPROM address map. We keep our
# own copies (independent of upstream availability) so the script degrades
# gracefully on a system without the rns package, AND so the byte values are
# documented and grep-able in this file.
# ---------------------------------------------------------------------------
class KISS:
    FEND            = 0xC0
    FESC            = 0xDB
    TFEND           = 0xDC
    TFESC           = 0xDD

    CMD_DATA        = 0x00
    CMD_FREQUENCY   = 0x01
    CMD_BANDWIDTH   = 0x02
    CMD_TXPOWER     = 0x03
    CMD_SF          = 0x04
    CMD_CR          = 0x05
    CMD_RADIO_STATE = 0x06
    CMD_DETECT      = 0x08
    CMD_LEAVE       = 0x0A
    CMD_FW_VERSION  = 0x50
    CMD_ROM_READ    = 0x51
    CMD_ROM_WRITE   = 0x52
    CMD_CONF_SAVE   = 0x53
    CMD_CONF_DELETE = 0x54
    CMD_RESET       = 0x55
    CMD_FW_HASH     = 0x58
    CMD_ROM_WIPE    = 0x59
    CMD_HASHES      = 0x60
    CMD_BT_PIN      = 0x62
    CMD_BT_CTRL     = 0x46
    CMD_PLATFORM    = 0x48
    CMD_MCU         = 0x49
    CMD_BOARD       = 0x47
    CMD_DEV_HASH    = 0x56

    DETECT_REQ      = 0x73
    DETECT_RESP     = 0x46

    @staticmethod
    def escape(data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if b == KISS.FEND:
                out += bytes([KISS.FESC, KISS.TFEND])
            elif b == KISS.FESC:
                out += bytes([KISS.FESC, KISS.TFESC])
            else:
                out.append(b)
        return bytes(out)


class ROM:
    """ROM.h constants. Required-identical to upstream rnodeconf.ROM."""
    ADDR_PRODUCT   = 0x00
    ADDR_MODEL     = 0x01
    ADDR_HW_REV    = 0x02
    ADDR_SERIAL    = 0x03
    ADDR_MADE      = 0x07
    ADDR_CHKSUM    = 0x0B
    ADDR_SIGNATURE = 0x1B
    ADDR_INFO_LOCK = 0x9B
    ADDR_CONF_SF   = 0x9C
    ADDR_CONF_CR   = 0x9D
    ADDR_CONF_TXP  = 0x9E
    ADDR_CONF_BW   = 0x9F
    ADDR_CONF_FREQ = 0xA3
    ADDR_CONF_OK   = 0xA7
    ADDR_CONF_BT   = 0xB0

    PLATFORM_NRF52 = 0x70
    MCU_NRF52      = 0x71
    PRODUCT_T1000E = 0x1E
    BOARD_T1000E   = 0x52

    INFO_LOCK_BYTE = 0x73
    CONF_OK_BYTE   = 0x73
    BT_ENABLE_BYTE = 0x73


# ---------------------------------------------------------------------------
# Per-board profile. The tool is built around a tiny table so a future board
# (XIAO, etc.) can be added without rewriting anything here.
# ---------------------------------------------------------------------------
class BoardProfile:
    """Static profile for a supported board. Independent of model string
    rnodeconf recognises -- this is the *physical* device's radio limits and
    flash+provision entry points for THIS fork's bundle."""

    def __init__(
        self,
        name: str,
        human: str,
        product: int,
        model: int,
        board: int,
        freq_min: int,
        freq_max: int,
        bw_min: int,
        bw_max: int,
        sf_min: int,
        sf_max: int,
        cr_min: int,
        cr_max: int,
        txp_min: int,
        txp_max: int,
        provision_script: str,
        default_firmware: str,
    ):
        self.name = name
        self.human = human
        self.product = product
        self.model = model
        self.board = board
        self.freq_min = freq_min
        self.freq_max = freq_max
        self.bw_min = bw_min
        self.bw_max = bw_max
        self.sf_min = sf_min
        self.sf_max = sf_max
        self.cr_min = cr_min
        self.cr_max = cr_max
        self.txp_min = txp_min
        self.txp_max = txp_max
        self.provision_script = provision_script
        self.default_firmware = default_firmware


# T1000-E: LR1110 radio, 863-928 MHz band (per the device card's README).
# SF/CR/TXP ranges pulled from the firmware's own lr1110.cpp clamps at
# the lora.cpp command dispatch (CMD_FREQUENCY/CMD_BANDWIDTH/CMD_SF/CMD_CR/
# CMD_TXPOWER): SF 5..12, CR 5..8 (denominator), TXP -17..22. BW range
# from the LR1110 setSignalBandwidth ladder (7.8 kHz .. 500 kHz).
T1000E = BoardProfile(
    name="t1000e",
    human="Seeed SenseCAP T1000-E (LR1110)",
    product=ROM.PRODUCT_T1000E,
    model=0xB5,
    board=ROM.BOARD_T1000E,
    freq_min=863_000_000,
    freq_max=928_000_000,
    bw_min=7_800,
    bw_max=500_000,
    sf_min=5,
    sf_max=12,
    cr_min=5,
    cr_max=8,
    txp_min=-17,
    txp_max=22,
    # provision_t1000e.sh lives in the same folder as the firmware.
    # The path is resolved against the repo root (parent of tools/).
    provision_script="Seeed Studio/SENSECAP T1000-E/provision_t1000e.sh",
    default_firmware="firmware/rnode_firmware_t1000e.zip",
)

BOARDS: dict[str, BoardProfile] = {
    T1000E.name: T1000E,
}


# ---------------------------------------------------------------------------
# Field table. One entry per independent KISS settable. The field table is
# what `-h` (help) and `configure` argument parsing consume.
# ---------------------------------------------------------------------------
class Field:
    """One settable radio parameter."""

    def __init__(self, key: str, flag: str, label: str, unit: str,
                 mn: int, mx: int, hint: str = "",
                 setter: Optional[str] = None):
        self.key = key
        self.flag = flag
        self.label = label
        self.unit = unit
        self.mn = mn
        self.mx = mx
        self.hint = hint
        # The SerialSession method name used to write this field. Defaults
        # to f"set_{key}" but can be overridden when the key name doesn't
        # match the underlying method (e.g. key="bw" -> method="set_bandwidth").
        self.setter = setter or f"set_{key}"

    def validate(self, value: int, board: BoardProfile) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{self.label}: must be an integer")
        if value < self.mn or value > self.mx:
            raise ValueError(
                f"{self.label}: {value} {self.unit} out of range "
                f"[{self.mn}, {self.mx}] for {board.human}"
            )


def make_fields(board: BoardProfile) -> list[Field]:
    """Build the field table for a given board profile. The table is built
    per-board from the profile's range constants so a future board's table
    is generated automatically."""
    return [
        Field("freq", "--freq", "Frequency", "Hz",
              board.freq_min, board.freq_max,
              "carrier frequency in Hz (e.g. 915000000)"),
        Field("bw", "--bw", "Bandwidth", "Hz",
              board.bw_min, board.bw_max,
              "LoRa signal bandwidth in Hz (e.g. 125000)",
              setter="set_bandwidth"),
        Field("sf", "--sf", "Spreading factor", "",
              board.sf_min, board.sf_max,
              f"LoRa SF range for {board.human}"),
        Field("cr", "--cr", "Coding rate", "",
              board.cr_min, board.cr_max,
              "LoRa coding rate *denominator* (5..8; firmware treats as 4/CR)"),
        Field("txp", "--txp", "TX power", "dBm",
              board.txp_min, board.txp_max,
              f"transmit power in dBm (range is firmware-clamped)",
              setter="set_txpower"),
    ]


# ---------------------------------------------------------------------------
# Port autodetection. Pure stdlib; doesn't open / touch anything.
# ---------------------------------------------------------------------------
def detect_ports(dev_root: str = "/dev") -> list[str]:
    """Return available /dev/ttyACM* and /dev/ttyUSB* paths under ``dev_root``.

    Sorted: ACM0, ACM1, ..., USB0, USB1, ... (matches the sibling tool's
    notion of "stable" enumeration order).

    This is purely a glob + exist-check; nothing is opened. Pass a tempdir
    in tests to fake the port list without touching real hardware.
    """
    dev_root = dev_root.rstrip("/") or "/"
    candidates: list[str] = []
    for prefix in ("ttyACM", "ttyUSB"):
        # Two globs: one in case dev_root is a placeholder like "/dev",
        # one for explicit enumeration from /dev (the common case).
        try:
            entries = sorted(os.listdir(dev_root))
        except FileNotFoundError:
            entries = []
        # Match exact "ttyACMxx" / "ttyUSBxx" -- not symlinks, not subdirs.
        for name in entries:
            if not name.startswith(prefix):
                continue
            if not name[len(prefix):].isdigit():
                continue
            path = os.path.join(dev_root, name)
            if os.path.exists(path):
                candidates.append(path)
    return candidates


def select_port(dev_root: str = "/dev", requested: Optional[str] = None,
                interactive: bool = True, stdin=None, stdout=None) -> str:
    """Resolve the device port to use.

    - If ``requested`` is set, return it (after a sanity check).
    - Otherwise, autodetect under ``dev_root``.
      - 0 found: error out.
      - 1 found: return it.
      - >1 found: prompt for a numbered choice if ``interactive``; else
        error out with the list.

    Tests pass a tempdir and ``interactive=False`` to avoid any TTY access.
    """
    if requested:
        if not os.path.exists(requested):
            raise FileNotFoundError(f"requested device not found: {requested}")
        return requested

    ports = detect_ports(dev_root)
    if not ports:
        raise FileNotFoundError(
            f"no /dev/ttyACM* or /dev/ttyUSB* ports found under {dev_root!r}; "
            f"pass --dev explicitly or plug the device in"
        )
    if len(ports) == 1:
        return ports[0]
    if not interactive:
        raise RuntimeError(
            f"multiple ports found: {ports}; pass --dev <port> to disambiguate"
        )
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    print(f"Multiple candidate ports detected:", file=stdout)
    for i, p in enumerate(ports, 1):
        print(f"  {i}) {p}", file=stdout)
    while True:
        print(f"Select port [1-{len(ports)}] (or 'q' to quit): ", file=stdout, end="", flush=True)
        line = stdin.readline()
        if not line:
            raise EOFError("no selection")
        line = line.strip()
        if line.lower() in ("q", "quit", "exit"):
            raise SystemExit("aborted by user")
        try:
            idx = int(line)
        except ValueError:
            print(f"  not a number: {line!r}", file=stdout)
            continue
        if 1 <= idx <= len(ports):
            return ports[idx - 1]
        print(f"  out of range: {idx}", file=stdout)


# ---------------------------------------------------------------------------
# Serial session. Wraps the open / detect / read / write lifecycle.
# ---------------------------------------------------------------------------
class SerialError(Exception):
    """A KISS-level error interacting with the device."""


class SerialSession:
    """High-level KISS session that dials a manager-style host talking to
    a T1000-E (or compatible) nRF52 board running RNode firmware.

    The session is *discovery + light writes only* -- it does not run a
    full background readLoop forever. Per-call, it does a brief pulse of
    readLoop on a worker thread to capture the responses to the queries
    it just sent, then exits the loop cleanly.

    Tests pass a :class:`MockSerial` instance in place of a real pyserial
    Serial object (the contract is ``.write`` returning a byte count and
    ``.in_waiting`` / ``.read(N)`` / ``.is_open`` / ``.close`` working).
    """

    READLOOP_PULSE_S = 0.8   # how long readLoop runs per pulse
    EEPROM_PULSE_S   = 1.0   # longer for the 200-byte EEPROM dump

    def __init__(self, serial_obj: Any, board: BoardProfile = T1000E,
                 write_only: bool = False):
        self.serial = serial_obj
        self.board = board
        self.write_only = write_only

        # Live state, populated by pulses of readLoop.
        self.detected: Optional[bool] = None
        self.firmware_version: Optional[str] = None
        self.platform: Optional[int] = None
        self.mcu: Optional[int] = None
        self.board_id: Optional[int] = None
        self.device_hash: Optional[bytes] = None
        self.firmware_hash: Optional[bytes] = None
        self.firmware_hash_target: Optional[bytes] = None

        # r_* = last value reported by the firmware for the live radio
        # settings (queried via CMD_FREQUENCY with 0, CMD_SF with 0xFF, etc.)
        self.r_frequency: Optional[int] = None
        self.r_bandwidth: Optional[int] = None
        self.r_txpower: Optional[int] = None
        self.r_sf: Optional[int] = None
        self.r_cr: Optional[int] = None
        self.r_bt_pin: Optional[int] = None

        # Configured values from the EEPROM (RNode.conf_* fields).
        self.conf_sf: Optional[int] = None
        self.conf_cr: Optional[int] = None
        self.conf_txpower: Optional[int] = None
        self.conf_bandwidth: Optional[int] = None
        self.conf_frequency: Optional[int] = None
        self.conf_ok: bool = False

        # Provisioned state parsed from the EEPROM info region.
        self.provisioned: Optional[bool] = None
        self.checksum_ok: Optional[bool] = None
        self.product: Optional[int] = None
        self.model: Optional[int] = None
        self.hw_rev: Optional[int] = None
        self.serialno: Optional[bytes] = None
        self.made: Optional[bytes] = None
        self.info_lock: Optional[int] = None
        self.eeprom: Optional[bytes] = None

    # ---- low-level framing -------------------------------------------------

    def _write_frame(self, command: int, payload: bytes = b"") -> None:
        frame = bytes([KISS.FEND, command]) + KISS.escape(payload) + bytes([KISS.FEND])
        n = self.serial.write(frame)
        if n != len(frame):
            raise SerialError(f"short write: {n} of {len(frame)} bytes")

    def _read_loop_pulse(self, duration_s: float) -> None:
        """Run a background readLoop for at most ``duration_s`` seconds.

        We re-implement just the EEPROM-detection branch of readLoop() here
        so we don't pull in all of upstream RNode (and don't trigger its
        hard-exit behavior on checksum mismatch).
        """
        if getattr(self.serial, "is_open", True) is False:
            return

        stop_at = time.monotonic() + max(0.0, duration_s)
        in_frame = False
        command = 0xFE
        data_buffer = bytearray()
        command_buffer = bytearray()
        escape = False

        while time.monotonic() < stop_at:
            try:
                waiting = self.serial.in_waiting
            except Exception:
                waiting = 0
            if waiting:
                raw = self.serial.read(min(waiting, 256))
                for byte in raw:
                    if in_frame and byte == KISS.FEND and command == KISS.CMD_ROM_READ:
                        self.eeprom = bytes(data_buffer)
                        in_frame = False
                        data_buffer = bytearray()
                        command_buffer = bytearray()
                    elif byte == KISS.FEND:
                        in_frame = True
                        command = 0xFE
                        data_buffer = bytearray()
                        command_buffer = bytearray()
                        escape = False
                    elif in_frame:
                        if len(data_buffer) >= 1024:
                            # overflow guard, same as upstream
                            break
                        if len(data_buffer) == 0 and command == 0xFE:
                            command = byte
                        elif command == KISS.CMD_ROM_READ:
                            if escape:
                                byte = {KISS.TFEND: KISS.FEND,
                                        KISS.TFESC: KISS.FESC}.get(byte, byte)
                                escape = False
                            elif byte == KISS.FESC:
                                escape = True
                                continue
                            data_buffer.append(byte)
                        elif command == KISS.CMD_FREQUENCY:
                            if escape:
                                byte = {KISS.TFEND: KISS.FEND,
                                        KISS.TFESC: KISS.FESC}.get(byte, byte)
                                escape = False
                            elif byte == KISS.FESC:
                                escape = True
                                continue
                            command_buffer.append(byte)
                            if len(command_buffer) == 4:
                                self.r_frequency = (
                                    command_buffer[0] << 24
                                    | command_buffer[1] << 16
                                    | command_buffer[2] << 8
                                    | command_buffer[3]
                                )
                                command_buffer = bytearray()
                        elif command == KISS.CMD_BANDWIDTH:
                            if escape:
                                byte = {KISS.TFEND: KISS.FEND,
                                        KISS.TFESC: KISS.FESC}.get(byte, byte)
                                escape = False
                            elif byte == KISS.FESC:
                                escape = True
                                continue
                            command_buffer.append(byte)
                            if len(command_buffer) == 4:
                                self.r_bandwidth = (
                                    command_buffer[0] << 24
                                    | command_buffer[1] << 16
                                    | command_buffer[2] << 8
                                    | command_buffer[3]
                                )
                                command_buffer = bytearray()
                        elif command == KISS.CMD_BT_PIN:
                            if escape:
                                byte = {KISS.TFEND: KISS.FEND,
                                        KISS.TFESC: KISS.FESC}.get(byte, byte)
                                escape = False
                            elif byte == KISS.FESC:
                                escape = True
                                continue
                            command_buffer.append(byte)
                            if len(command_buffer) == 4:
                                self.r_bt_pin = (
                                    command_buffer[0] << 24
                                    | command_buffer[1] << 16
                                    | command_buffer[2] << 8
                                    | command_buffer[3]
                                )
                                command_buffer = bytearray()
                        elif command == KISS.CMD_DETECT:
                            if byte == KISS.DETECT_RESP:
                                self.detected = True
                            else:
                                self.detected = False
                        elif command == KISS.CMD_FW_VERSION:
                            command_buffer.append(byte)
                            if len(command_buffer) == 2:
                                self.firmware_version = (
                                    f"{command_buffer[0]}.{command_buffer[1]:02d}"
                                )
                                command_buffer = bytearray()
                        elif command == KISS.CMD_PLATFORM:
                            self.platform = byte
                        elif command == KISS.CMD_MCU:
                            self.mcu = byte
                        elif command == KISS.CMD_BOARD:
                            self.board_id = byte
                        elif command == KISS.CMD_DEV_HASH:
                            if escape:
                                byte = {KISS.TFEND: KISS.FEND,
                                        KISS.TFESC: KISS.FESC}.get(byte, byte)
                                escape = False
                            elif byte == KISS.FESC:
                                escape = True
                                continue
                            command_buffer.append(byte)
                            if len(command_buffer) == 32:
                                self.device_hash = bytes(command_buffer)
                                command_buffer = bytearray()
                        elif command == KISS.CMD_HASHES:
                            if escape:
                                byte = {KISS.TFEND: KISS.FEND,
                                        KISS.TFESC: KISS.FESC}.get(byte, byte)
                                escape = False
                            elif byte == KISS.FESC:
                                escape = True
                                continue
                            command_buffer.append(byte)
                            if len(command_buffer) == 33:
                                kind = command_buffer[0]
                                payload = bytes(command_buffer[1:])
                                if kind == 0x01:
                                    self.firmware_hash_target = payload
                                elif kind == 0x02:
                                    self.firmware_hash = payload
                                command_buffer = bytearray()
                        elif command == KISS.CMD_TXPOWER:
                            self.r_txpower = byte
                        elif command == KISS.CMD_SF:
                            self.r_sf = byte
                        elif command == KISS.CMD_CR:
                            self.r_cr = byte
            else:
                # Be a polite citizen on real ports; tests inject canned
                # bytes and don't need a sleep.
                if hasattr(self.serial, "_is_mock") and self.serial._is_mock:
                    break
                time.sleep(0.005)

    # ---- public commands ---------------------------------------------------

    def detect(self) -> None:
        """CMD_DETECT + the firmware info requests. Mirrors RNode.detect()."""
        frame = (
            bytes([KISS.FEND, KISS.CMD_DETECT, KISS.DETECT_REQ, KISS.FEND])
            + bytes([KISS.FEND, KISS.CMD_FW_VERSION, 0x00, KISS.FEND])
            + bytes([KISS.FEND, KISS.CMD_PLATFORM, 0x00, KISS.FEND])
            + bytes([KISS.FEND, KISS.CMD_MCU, 0x00, KISS.FEND])
            + bytes([KISS.FEND, KISS.CMD_BOARD, 0x00, KISS.FEND])
            + bytes([KISS.FEND, KISS.CMD_DEV_HASH, 0x01, KISS.FEND])
            + bytes([KISS.FEND, KISS.CMD_HASHES, 0x01, KISS.FEND])
            + bytes([KISS.FEND, KISS.CMD_HASHES, 0x02, KISS.FEND])
        )
        n = self.serial.write(frame)
        if n != len(frame):
            raise SerialError(f"detect: short write {n}/{len(frame)}")
        self._read_loop_pulse(self.READLOOP_PULSE_S)

    def query_live_settings(self) -> None:
        """Query the firmware for current radio settings via the
        "report current value" KISS commands.

        The firmware treats:
            - CMD_FREQUENCY  + 4 zero bytes  -> report current frequency
            - CMD_BANDWIDTH  + 4 zero bytes  -> report current bandwidth
            - CMD_TXPOWER    + 0xFF          -> report current TX power
            - CMD_SF         + 0xFF          -> report current SF
            - CMD_CR         + 0xFF          -> report current CR

        (See lr1110.cpp / RNode_Firmware.ino.cpp handler for each.)
        """
        self._write_frame(KISS.CMD_FREQUENCY, b"\x00\x00\x00\x00")
        self._write_frame(KISS.CMD_BANDWIDTH, b"\x00\x00\x00\x00")
        self._write_frame(KISS.CMD_TXPOWER, b"\xff")
        self._write_frame(KISS.CMD_SF, b"\xff")
        self._write_frame(KISS.CMD_CR, b"\xff")
        self._read_loop_pulse(self.READLOOP_PULSE_S)

    def download_eeprom(self) -> bytes:
        """Send CMD_ROM_READ and capture the EEPROM dump. Returns the raw
        bytes (escaping is unescaped by readLoop). Does NOT call
        rnodeconf.parse_eeprom() -- which would hard-exit on any
        checksum mismatch -- we parse it ourselves below."""
        self.eeprom = None
        self._write_frame(KISS.CMD_ROM_READ, b"\x00")
        self._read_loop_pulse(self.EEPROM_PULSE_S)
        if self.eeprom is None:
            raise SerialError("could not download EEPROM (no FEND response)")
        self.parse_eeprom_safe(self.eeprom)
        return self.eeprom

    def parse_eeprom_safe(self, dump: bytes) -> None:
        """Parse the 200-byte EEPROM dump *without* calling graceful_exit().
        Sets self.product/model/hw_rev/serialno/made/info_lock/conf_*/checksum_ok
        /provisioned. A checksum mismatch is *not* fatal here -- the operator
        (caller) decides what to do with that information."""
        if len(dump) < 0xA8:
            raise SerialError(f"EEPROM dump too short: {len(dump)} bytes")

        try:
            self.product = dump[ROM.ADDR_PRODUCT]
            self.model = dump[ROM.ADDR_MODEL]
            self.hw_rev = dump[ROM.ADDR_HW_REV]
            self.serialno = bytes(dump[ROM.ADDR_SERIAL:ROM.ADDR_SERIAL + 4])
            self.made = bytes(dump[ROM.ADDR_MADE:ROM.ADDR_MADE + 4])
            self.info_lock = dump[ROM.ADDR_INFO_LOCK]

            checksummed = (
                bytes([self.product, self.model, self.hw_rev])
                + self.serialno + self.made
            )
            stored = bytes(dump[ROM.ADDR_CHKSUM:ROM.ADDR_CHKSUM + 16])
            computed = hashlib.md5(checksummed).digest()
            self.checksum_ok = (stored == computed)

            self.provisioned = (self.info_lock == ROM.INFO_LOCK_BYTE)

            self.conf_sf = dump[ROM.ADDR_CONF_SF]
            self.conf_cr = dump[ROM.ADDR_CONF_CR]
            self.conf_txpower = dump[ROM.ADDR_CONF_TXP]
            self.conf_bandwidth = struct.unpack(
                ">I", dump[ROM.ADDR_CONF_BW:ROM.ADDR_CONF_BW + 4]
            )[0]
            self.conf_frequency = struct.unpack(
                ">I", dump[ROM.ADDR_CONF_FREQ:ROM.ADDR_CONF_FREQ + 4]
            )[0]
            self.conf_ok = (dump[ROM.ADDR_CONF_OK] == ROM.CONF_OK_BYTE)
        except Exception as e:
            raise SerialError(f"EEPROM parse failed: {e}") from e

    def set_freq(self, freq: int) -> None:
        self._write_frame(KISS.CMD_FREQUENCY, struct.pack(">I", freq))

    def set_bandwidth(self, bw: int) -> None:
        self._write_frame(KISS.CMD_BANDWIDTH, struct.pack(">I", bw))

    def set_txpower(self, txp: int) -> None:
        # LR1110 firmware treats 0xFF as a query; we send the actual value.
        if not (0 <= txp <= 255):
            raise ValueError(f"txpower byte out of range: {txp}")
        self._write_frame(KISS.CMD_TXPOWER, bytes([txp & 0xFF]))

    def set_sf(self, sf: int) -> None:
        if not (0 <= sf <= 255):
            raise ValueError(f"sf byte out of range: {sf}")
        self._write_frame(KISS.CMD_SF, bytes([sf & 0xFF]))

    def set_cr(self, cr: int) -> None:
        if not (0 <= cr <= 255):
            raise ValueError(f"cr byte out of range: {cr}")
        self._write_frame(KISS.CMD_CR, bytes([cr & 0xFF]))

    def leave(self) -> None:
        self._write_frame(KISS.CMD_LEAVE, b"\xff")
        time.sleep(0.2)

    def wipe_eeprom(self) -> None:
        """CMD_ROM_WIPE 0xf8. The real firmware takes 13s+ to actually
        complete the wipe (the upstream library even sleeps 23s for RAK4631).
        We don't sleep here -- the caller is expected to wait, but the real
        port's underlying drain/re-enumeration happens asynchronously and
        the timeout is bound by the caller, not this method."""
        self._write_frame(KISS.CMD_ROM_WIPE, b"\xf8")

    def save_config(self) -> None:
        """CMD_CONF_SAVE makes the radio come up on boot with the current
        live settings. Optional but useful after `set`."""
        self._write_frame(KISS.CMD_CONF_SAVE, b"\x00")

    def clear_config(self) -> None:
        self._write_frame(KISS.CMD_CONF_DELETE, b"\x00")

    def reset(self) -> None:
        self._write_frame(KISS.CMD_RESET, b"\xf8")

    def close(self) -> None:
        try:
            self.serial.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI subcommands.
# ---------------------------------------------------------------------------
def _shared_parser(board_default: str = "t1000e") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--dev", default=None,
                   help="serial device path (e.g. /dev/ttyACM0); auto-detect if omitted")
    p.add_argument("--dev-root", default="/dev",
                   help="filesystem root to scan for /dev/ttyACM* and /dev/ttyUSB* (default: /dev)")
    p.add_argument("--board", default=board_default,
                   help=f"board profile name (choices: {', '.join(BOARDS.keys())})")
    p.add_argument("--yes", "-y", action="store_true",
                   help="skip the interactive confirmation prompt")
    return p


def _resolve_board(args: argparse.Namespace) -> BoardProfile:
    if args.board not in BOARDS:
        raise SystemExit(
            f"unknown board: {args.board!r}; "
            f"supported boards: {', '.join(BOARDS.keys())}"
        )
    return BOARDS[args.board]


def _open_serial(dev: str, baud: int = 115200) -> Any:
    """Open a real pyserial Serial. Lazy import so tests can pass a mock
    without requiring pyserial (it is required at runtime though)."""
    try:
        import serial as _serial
    except ImportError as e:
        raise SystemExit(
            "pyserial is required to talk to a real device. "
            "Install it with: pip install pyserial"
        ) from e
    return _serial.Serial(dev, baud, timeout=0.05)


def _open_session(dev: Optional[str], args: argparse.Namespace,
                  board: BoardProfile) -> SerialSession:
    """Resolve the port (interactive/autodetect if needed) and open a
    SerialSession. Tests call SerialSession(mock_serial) directly."""
    if dev is None:
        dev = select_port(args.dev_root, requested=None, interactive=sys.stdin.isatty())
    rv = _open_serial(dev)
    return SerialSession(rv, board=board)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------
def cmd_show(args: argparse.Namespace) -> int:
    board = _resolve_board(args)
    try:
        dev = args.dev or select_port(args.dev_root, requested=None,
                                      interactive=sys.stdin.isatty())
    except (FileNotFoundError, RuntimeError, EOFError) as e:
        print(f"rnode show: {e}", file=sys.stderr)
        return 2

    try:
        serial_obj = _open_serial(dev)
    except Exception as e:
        print(f"rnode show: could not open {dev}: {e}", file=sys.stderr)
        return 2
    s = SerialSession(serial_obj, board=board)
    try:
        s.detect()
        s.query_live_settings()
        try:
            s.download_eeprom()
        except SerialError as e:
            print(f"# EEPROM read failed: {e}", file=sys.stderr)
    finally:
        try:
            s.close()
        except Exception:
            pass

    # Pretty-print everything as a fixed-width text block. Mirrors the
    # sibling tool's "always prints something useful, even on partial
    # read" UX.
    out = sys.stdout
    hr = "-" * 60
    print(hr, file=out)
    print(f"Device:        {dev}", file=out)
    print(f"Board profile: {board.human} ({board.name})", file=out)
    print(hr, file=out)

    def row(label: str, value: Any) -> None:
        if value is None:
            vstr = "(unknown)"
        else:
            vstr = str(value)
        print(f"  {label:<26} {vstr}", file=out)

    print("Device identity", file=out)
    row("Detected", "yes" if s.detected else "no" if s.detected is False else "(no response)")
    row("Firmware version", s.firmware_version)
    row("Platform", _platform_name(s.platform))
    row("MCU", _mcu_name(s.mcu))
    row("Board ID", f"0x{s.board_id:02x}" if s.board_id is not None else None)
    if s.device_hash:
        row("Device hash", s.device_hash.hex())
    if s.firmware_hash:
        row("Firmware hash (live)", s.firmware_hash.hex())
    if s.firmware_hash_target:
        row("Firmware hash (target)", s.firmware_hash_target.hex())

    print("", file=out)
    print("Provisioning (EEPROM info region)", file=out)
    if s.eeprom is not None:
        row("Provisioned", "yes" if s.provisioned else "no")
        row("EEPROM checksum", "matches" if s.checksum_ok else "MISMATCH")
        row("Product", f"0x{s.product:02x}" if s.product is not None else None)
        row("Model", f"0x{s.model:02x}" if s.model is not None else None)
        row("Hardware rev", s.hw_rev)
        row("Serial", s.serialno.hex() if s.serialno else None)
        row("Made", s.made.hex() if s.made else None)
        row("Info lock byte", f"0x{s.info_lock:02x}" if s.info_lock is not None else None)
    else:
        print("  (no EEPROM retrieved)", file=out)

    print("", file=out)
    print("Current live radio settings (from device)", file=out)
    row("Frequency", f"{s.r_frequency} Hz" if s.r_frequency is not None else None)
    row("Bandwidth", f"{s.r_bandwidth} Hz" if s.r_bandwidth is not None else None)
    row("Spreading factor", s.r_sf)
    row("Coding rate", s.r_cr)
    row("TX power", f"{s.r_txpower} dBm" if s.r_txpower is not None else None)

    print("", file=out)
    print("Stored radio config (EEPROM)", file=out)
    if s.eeprom is not None:
        row("CONF_OK", "yes" if s.conf_ok else "no")
        row("Frequency", f"{s.conf_frequency} Hz" if s.conf_frequency is not None else None)
        row("Bandwidth", f"{s.conf_bandwidth} Hz" if s.conf_bandwidth is not None else None)
        row("Spreading factor", s.conf_sf)
        row("Coding rate", s.conf_cr)
        row("TX power", f"{s.conf_txpower} dBm" if s.conf_txpower is not None else None)
    else:
        print("  (no EEPROM retrieved)", file=out)

    if s.r_bt_pin is not None:
        print("", file=out)
        print(f"Bluetooth pairing PIN (device-side, randomly generated): {s.r_bt_pin:06d}", file=out)

    print(hr, file=out)
    return 0


# ---------------------------------------------------------------------------
# set (configure)
# ---------------------------------------------------------------------------
def cmd_set(args: argparse.Namespace) -> int:
    """Apply one or more radio settings. Argument names mirror the table
    in rnode.sh --help."""
    board = _resolve_board(args)
    fields = make_fields(board)
    by_key = {f.key: f for f in fields}

    # 1. Collect & validate the requested changes.
    requested: dict[str, int] = {}
    for f in fields:
        raw = getattr(args, f.key, None)
        if raw is None:
            continue
        try:
            value = int(raw, 0)
        except (TypeError, ValueError):
            raise SystemExit(f"--{f.key}: {raw!r} is not a valid integer")
        try:
            f.validate(value, board)
        except ValueError as e:
            raise SystemExit(str(e))
        requested[f.key] = value

    if not requested:
        raise SystemExit(
            "no settings to apply; pass at least one of: "
            + ", ".join(f"--{f.key}" for f in fields)
        )

    # 2. Open and read current state.
    try:
        dev = args.dev or select_port(args.dev_root, requested=None,
                                      interactive=sys.stdin.isatty())
    except (FileNotFoundError, RuntimeError, EOFError) as e:
        print(f"rnode configure: {e}", file=sys.stderr)
        return 2

    try:
        serial_obj = _open_serial(dev)
    except Exception as e:
        print(f"rnode configure: could not open {dev}: {e}", file=sys.stderr)
        return 2
    s = SerialSession(serial_obj, board=board)
    try:
        try:
            s.detect()
            s.query_live_settings()
        except SerialError as e:
            print(f"warning: could not query current settings: {e}", file=sys.stderr)

        # 3. Show the proposed change summary.
        print("Proposed change:", file=sys.stdout)
        for f in fields:
            if f.key not in requested:
                continue
            current = _current_value_for(s, f.key)
            print(f"  {f.label:<18}: {current} -> {requested[f.key]} {f.unit}", file=sys.stdout)

        # 4. Confirm. --yes skips; otherwise read a line.
        if not args.yes:
            print("Apply these changes? Type 'yes' to confirm: ", file=sys.stdout, end="", flush=True)
            line = sys.stdin.readline().strip().lower()
            if line not in ("yes", "y"):
                print("Aborted by user.", file=sys.stderr)
                return 1

        # 5. Apply. We write each one in order and trust the firmware to
        # accept it; if any write fails the bytes count will be short.
        for f in fields:
            if f.key not in requested:
                continue
            value = requested[f.key]
            method = getattr(s, f.setter)
            method(value)
        # Always leave cleanly so the radio state is consistent.
        s.leave()
    finally:
        try:
            s.close()
        except Exception:
            pass
    print("Done.", file=sys.stdout)
    return 0


# ---------------------------------------------------------------------------
# wipe
# ---------------------------------------------------------------------------
def cmd_wipe(args: argparse.Namespace) -> int:
    board = _resolve_board(args)
    if not args.yes:
        print("THIS WILL ERASE THE DEVICE EEPROM.", file=sys.stderr)
        print("This is IRREVERSIBLE -- the device will need to be re-provisioned", file=sys.stderr)
        print("via `rnode.sh flash` before it can be used again.", file=sys.stderr)
        print("Type WIPE (uppercase) to confirm: ", file=sys.stderr, end="", flush=True)
        line = sys.stdin.readline().strip()
        if line != "WIPE":
            print("Aborted by user.", file=sys.stderr)
            return 1

    try:
        dev = args.dev or select_port(args.dev_root, requested=None,
                                      interactive=sys.stdin.isatty())
    except (FileNotFoundError, RuntimeError, EOFError) as e:
        print(f"rnode wipe: {e}", file=sys.stderr)
        return 2

    try:
        serial_obj = _open_serial(dev)
    except Exception as e:
        print(f"rnode wipe: could not open {dev}: {e}", file=sys.stderr)
        return 2
    s = SerialSession(serial_obj, board=board)
    try:
        s.wipe_eeprom()
    finally:
        try:
            s.close()
        except Exception:
            pass
    print("EEPROM wiped. Re-provision with `rnode.sh flash` before use.", file=sys.stdout)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _platform_name(p: Optional[int]) -> Optional[str]:
    return {
        0x70: "NRF52", 0x80: "ESP32", 0x90: "AVR",
    }.get(p, None)


def _mcu_name(m: Optional[int]) -> Optional[str]:
    return {
        0x71: "nRF52840", 0x81: "ESP32", 0x91: "ATmega1284P", 0x92: "ATmega2560",
    }.get(m, None)


def _current_value_for(s: SerialSession, key: str) -> str:
    """Pretty-print the current value for `--key` (a summary line)."""
    if key == "freq":
        return f"{s.r_frequency} Hz" if s.r_frequency is not None else "(unknown)"
    if key == "bw":
        return f"{s.r_bandwidth} Hz" if s.r_bandwidth is not None else "(unknown)"
    if key == "sf":
        return str(s.r_sf) if s.r_sf is not None else "(unknown)"
    if key == "cr":
        return str(s.r_cr) if s.r_cr is not None else "(unknown)"
    if key == "txp":
        return f"{s.r_txpower} dBm" if s.r_txpower is not None else "(unknown)"
    return "(unknown)"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rnode_serial.py",
        description="KISS/RNode helper for the T1000-E (and compatible) fork.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # show
    p_show = sub.add_parser("show", parents=[_shared_parser()],
                            help="read and print device state")
    p_show.set_defaults(func=cmd_show)

    # set
    p_set = sub.add_parser("set", parents=[_shared_parser()],
                           help="write one or more radio parameters")
    for f in make_fields(T1000E):
        p_set.add_argument(f.flag, dest=f.key, default=None,
                           help=f"{f.label} ({f.unit}) [{f.mn}..{f.mx}] -- {f.hint}")
    p_set.add_argument("--save", dest="save", action="store_true",
                       help="persist current settings as the boot config (CMD_CONF_SAVE)")
    p_set.set_defaults(func=cmd_set)

    # wipe
    p_wipe = sub.add_parser("wipe", parents=[_shared_parser()],
                            help="erase the device EEPROM (destructive)")
    p_wipe.set_defaults(func=cmd_wipe)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
