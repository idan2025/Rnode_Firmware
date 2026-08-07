#!/usr/bin/env python3
"""Tests for tools/rnode_serial.py — fully mocked, no real serial device.

Run with:
    python3 tools/test_rnode_serial.py

These tests do NOT touch any real /dev/ttyACM* or /dev/ttyUSB* device. They
use a MockSerial class that records bytes written and returns canned
responses on read. There is also a synthetic EEPROM-dump helper that
constructs a 200-byte dump identical in layout to what a real T1000-E
provisioned by `provision_t1000e.sh` would produce.

The test runner is intentionally minimal (no pytest) so it can be invoked
without setting up a venv / installing pytest. The exit code is non-zero
if any test fails.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from typing import Optional

# Make rnode_serial.py importable without needing to install it.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, "/home/josh/reticulum-stack/venv/lib/python3.13/site-packages")

import rnode_serial  # noqa: E402


# ---------------------------------------------------------------------------
# MockSerial: a fake pyserial.Serial that records writes and replays
# canned responses on read. The replay is "all at once" (everything in
# the canned buffer is returned on the first read) which is the simplest
# model that exercises the readLoop's frame parser.
# ---------------------------------------------------------------------------
class MockSerial:
    """Fake pyserial.Serial for tests.

    Records every .write() call in `self.writes` (one entry per call,
    as the bytes that were passed). On .read(n) it pops up to n bytes
    from `self._rx_buffer` (which the test pre-loads with canned bytes).
    On .in_waiting it returns the number of bytes left in the buffer.

    Attributes that tests may inspect:
        .writes: list[bytes]               -- history of writes
        .is_open: bool                    -- always True until close()
        .name: str                        -- fake path
    """

    def __init__(self, name: str = "/dev/ttyACM0"):
        self.name = name
        self.writes: list[bytes] = []
        self._rx_buffer: bytes = b""
        self.is_open = True
        # Tests can set this to skip the readLoop's polite sleep.
        self._is_mock = True

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, n: int = 1) -> bytes:
        if not self._rx_buffer:
            return b""
        out = self._rx_buffer[:n]
        self._rx_buffer = self._rx_buffer[n:]
        return out

    def inject(self, payload: bytes) -> None:
        """Append bytes to the RX buffer (the test's main lever)."""
        self._rx_buffer += payload

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        return len(self._rx_buffer)

    def close(self) -> None:
        self.is_open = False

    # ---------- assertions helpers ----------
    def write_count(self) -> int:
        return len(self.writes)

    def all_writes_bytes(self) -> bytes:
        return b"".join(self.writes)

    def find_frame(self, marker: bytes) -> Optional[int]:
        """Return the index of the first .write() containing marker, or None."""
        for i, w in enumerate(self.writes):
            if marker in w:
                return i
        return None


# ---------------------------------------------------------------------------
# Helpers for building KISS frames and a realistic provisioned EEPROM dump.
# ---------------------------------------------------------------------------
def _kiss_frame(command: int, payload: bytes = b"") -> bytes:
    return bytes([rnode_serial.KISS.FEND, command]) + rnode_serial.KISS.escape(payload) + bytes([rnode_serial.KISS.FEND])


def _makesynthetic_eeprom_dump(
    freq: int = 915_000_000,
    bw: int = 125_000,
    sf: int = 7,
    cr: int = 5,
    txp: int = 14,
    conf_ok: bool = True,
    product: int = 0x1E,
    model: int = 0xB5,
    hw_rev: int = 1,
    serial: bytes = b"\x01\x02\x03\x04",
    made: bytes = b"\x00\x00\x00\x01",
    bt_enabled: bool = True,
    info_locked: bool = True,
) -> bytes:
    """Build a 200-byte EEPROM dump that mimics a real T1000-E provisioned
    by `provision_t1000e.sh` (which uses `rnodeconf -r --product 1e --model
    b5 --hwrev 1`). The dump is laid out per ROM.h's ADDR_* constants and
    the standard MD5-checksummed info region."""
    import struct
    dump = bytearray(b"\x00" * 200)

    # Info region (ADDR_PRODUCT..ADDR_MADE+4) = 11 bytes
    dump[rnode_serial.ROM.ADDR_PRODUCT] = product
    dump[rnode_serial.ROM.ADDR_MODEL] = model
    dump[rnode_serial.ROM.ADDR_HW_REV] = hw_rev
    dump[rnode_serial.ROM.ADDR_SERIAL:rnode_serial.ROM.ADDR_SERIAL + 4] = serial
    dump[rnode_serial.ROM.ADDR_MADE:rnode_serial.ROM.ADDR_MADE + 4] = made

    # MD5 checksum over the 11 info bytes
    info = bytes([product, model, hw_rev]) + serial + made
    assert len(info) == 0x0B
    chk = hashlib.md5(info).digest()
    dump[rnode_serial.ROM.ADDR_CHKSUM:rnode_serial.ROM.ADDR_CHKSUM + 16] = chk

    # Config sector
    dump[rnode_serial.ROM.ADDR_CONF_SF] = sf
    dump[rnode_serial.ROM.ADDR_CONF_CR] = cr
    dump[rnode_serial.ROM.ADDR_CONF_TXP] = txp
    dump[rnode_serial.ROM.ADDR_CONF_BW:rnode_serial.ROM.ADDR_CONF_BW + 4] = struct.pack(">I", bw)
    dump[rnode_serial.ROM.ADDR_CONF_FREQ:rnode_serial.ROM.ADDR_CONF_FREQ + 4] = struct.pack(">I", freq)
    if conf_ok:
        dump[rnode_serial.ROM.ADDR_CONF_OK] = rnode_serial.ROM.CONF_OK_BYTE

    # Bluetooth enable byte
    if bt_enabled:
        dump[rnode_serial.ROM.ADDR_CONF_BT] = rnode_serial.ROM.BT_ENABLE_BYTE

    # Info lock byte
    if info_locked:
        dump[rnode_serial.ROM.ADDR_INFO_LOCK] = rnode_serial.ROM.INFO_LOCK_BYTE

    return bytes(dump)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
class TestKISSFraming(unittest.TestCase):
    """Verify our KISS.escape matches upstream's behaviour for the bytes
    we care about (FEND, FESC + arbitrary payload)."""

    def test_escape_fend_fesc(self):
        esc = rnode_serial.KISS.escape
        self.assertEqual(esc(b"\xc0"), bytes([0xDB, 0xDC]))
        self.assertEqual(esc(b"\xdb"), bytes([0xDB, 0xDD]))
        self.assertEqual(esc(b"hello"), b"hello")
        self.assertEqual(esc(b"\xc0\xdb\xff"), bytes([0xDB, 0xDC, 0xDB, 0xDD, 0xFF]))

    def test_frame_roundtrip(self):
        # Manual unescape via the same logic the readLoop uses.
        def unesc(p):
            out, e = bytearray(), False
            for b in p:
                if e:
                    out.append(0xC0 if b == 0xDC else 0xDB if b == 0xDD else b)
                    e = False
                elif b == 0xDB:
                    e = True
                else:
                    out.append(b)
            return bytes(out)
        payload = bytes([0x01, 0x02, 0xC0, 0xDB, 0xFF])
        frame = _kiss_frame(0x51, payload)
        # Frame: FEND 0x51 esc(payload) FEND
        # Strip the outer FENDs and the command byte.
        inner = frame[2:-1]
        self.assertEqual(unesc(inner), payload)


class TestFieldValidation(unittest.TestCase):
    def test_freq_in_range(self):
        f = rnode_serial.make_fields(rnode_serial.T1000E)[0]
        f.validate(915_000_000, rnode_serial.T1000E)  # ok

    def test_freq_out_of_range(self):
        f = rnode_serial.make_fields(rnode_serial.T1000E)[0]
        with self.assertRaises(ValueError):
            f.validate(100_000_000, rnode_serial.T1000E)  # too low
        with self.assertRaises(ValueError):
            f.validate(2_400_000_000, rnode_serial.T1000E)  # too high

    def test_sf_range(self):
        sf_field = next(f for f in rnode_serial.make_fields(rnode_serial.T1000E) if f.key == "sf")
        sf_field.validate(5, rnode_serial.T1000E)
        sf_field.validate(12, rnode_serial.T1000E)
        with self.assertRaises(ValueError):
            sf_field.validate(4, rnode_serial.T1000E)
        with self.assertRaises(ValueError):
            sf_field.validate(13, rnode_serial.T1000E)

    def test_txp_range(self):
        txp_field = next(f for f in rnode_serial.make_fields(rnode_serial.T1000E) if f.key == "txp")
        txp_field.validate(-17, rnode_serial.T1000E)
        txp_field.validate(22, rnode_serial.T1000E)
        with self.assertRaises(ValueError):
            txp_field.validate(-18, rnode_serial.T1000E)
        with self.assertRaises(ValueError):
            txp_field.validate(23, rnode_serial.T1000E)

    def test_cr_range(self):
        cr_field = next(f for f in rnode_serial.make_fields(rnode_serial.T1000E) if f.key == "cr")
        cr_field.validate(5, rnode_serial.T1000E)
        cr_field.validate(8, rnode_serial.T1000E)
        with self.assertRaises(ValueError):
            cr_field.validate(4, rnode_serial.T1000E)
        with self.assertRaises(ValueError):
            cr_field.validate(9, rnode_serial.T1000E)


class TestPortAutodetect(unittest.TestCase):
    def _make_fake_dev(self, *entries: str) -> str:
        """Create a tempdir pre-populated with fake /dev/ttyACM* and
        /dev/ttyUSB* files. Returns the path (usable as --dev-root)."""
        tmp = tempfile.mkdtemp(prefix="rnode_test_")
        for name in entries:
            open(os.path.join(tmp, name), "w").close()
        self.addCleanup(self._cleanup_tmp, tmp)
        return tmp

    @staticmethod
    def _cleanup_tmp(tmp: str) -> None:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_no_ports(self):
        tmp = self._make_fake_dev()
        self.assertEqual(rnode_serial.detect_ports(tmp), [])

    def test_one_port(self):
        tmp = self._make_fake_dev("ttyACM0")
        self.assertEqual(rnode_serial.detect_ports(tmp), [os.path.join(tmp, "ttyACM0")])

    def test_multiple_ports_acm_first(self):
        tmp = self._make_fake_dev("ttyACM0", "ttyACM1", "ttyUSB0")
        result = rnode_serial.detect_ports(tmp)
        self.assertEqual(len(result), 3)
        # ACM comes before USB in the sorted order.
        self.assertTrue(result[0].endswith("ttyACM0"))
        self.assertTrue(result[1].endswith("ttyACM1"))
        self.assertTrue(result[2].endswith("ttyUSB0"))

    def test_ignores_non_matching(self):
        tmp = self._make_fake_dev("ttyACM0", "ttyS0", "ttyPRINTK", "foo")
        result = rnode_serial.detect_ports(tmp)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].endswith("ttyACM0"))

    def test_select_port_one(self):
        tmp = self._make_fake_dev("ttyACM0")
        self.assertEqual(rnode_serial.select_port(dev_root=tmp, interactive=False),
                         os.path.join(tmp, "ttyACM0"))

    def test_select_port_zero_errors(self):
        tmp = self._make_fake_dev()
        with self.assertRaises(FileNotFoundError):
            rnode_serial.select_port(dev_root=tmp, interactive=False)

    def test_select_port_multiple_non_interactive(self):
        tmp = self._make_fake_dev("ttyACM0", "ttyACM1")
        with self.assertRaises(RuntimeError):
            rnode_serial.select_port(dev_root=tmp, interactive=False)

    def test_select_port_multiple_with_stdin(self):
        tmp = self._make_fake_dev("ttyACM0", "ttyACM1")
        fake_input = io.StringIO("2\n")
        fake_output = io.StringIO()
        self.assertEqual(
            rnode_serial.select_port(dev_root=tmp, interactive=True,
                                     stdin=fake_input, stdout=fake_output),
            os.path.join(tmp, "ttyACM1"),
        )
        self.assertIn("Multiple candidate ports", fake_output.getvalue())

    def test_select_port_explicit(self):
        tmp = self._make_fake_dev("ttyACM0")
        self.assertEqual(
            rnode_serial.select_port(dev_root=tmp, requested=os.path.join(tmp, "ttyACM0"),
                                     interactive=False),
            os.path.join(tmp, "ttyACM0"),
        )

    def test_select_port_explicit_missing(self):
        with self.assertRaises(FileNotFoundError):
            rnode_serial.select_port(requested="/nonexistent/port", interactive=False)


class TestSerialSessionSet(unittest.TestCase):
    """Verify configure/cmd_set writes the correct KISS frames for each field."""

    def _new_session(self):
        mock = MockSerial()
        return mock, rnode_serial.SerialSession(mock, board=rnode_serial.T1000E)

    def test_set_freq(self):
        mock, s = self._new_session()
        s.set_freq(915_000_000)
        # Expected frame: FEND 0x01 [4-byte BE int] FEND
        freq_bytes = (915_000_000).to_bytes(4, "big")
        expected = bytes([0xC0, 0x01]) + rnode_serial.KISS.escape(freq_bytes) + bytes([0xC0])
        self.assertEqual(mock.writes, [expected])

    def test_set_bandwidth(self):
        mock, s = self._new_session()
        s.set_bandwidth(125_000)
        bw_bytes = (125_000).to_bytes(4, "big")
        expected = bytes([0xC0, 0x02]) + rnode_serial.KISS.escape(bw_bytes) + bytes([0xC0])
        self.assertEqual(mock.writes, [expected])

    def test_set_sf(self):
        mock, s = self._new_session()
        s.set_sf(7)
        self.assertEqual(mock.writes, [bytes([0xC0, 0x04, 0x07, 0xC0])])

    def test_set_cr(self):
        mock, s = self._new_session()
        s.set_cr(5)
        self.assertEqual(mock.writes, [bytes([0xC0, 0x05, 0x05, 0xC0])])

    def test_set_txpower(self):
        mock, s = self._new_session()
        s.set_txpower(14)
        self.assertEqual(mock.writes, [bytes([0xC0, 0x03, 0x0E, 0xC0])])

    def test_set_txpower_clamps_to_byte(self):
        mock, s = self._new_session()
        s.set_txpower(0xFF)
        self.assertEqual(mock.writes, [bytes([0xC0, 0x03, 0xFF, 0xC0])])

    def test_set_sf_rejects_out_of_byte(self):
        _, s = self._new_session()
        with self.assertRaises(ValueError):
            s.set_sf(256)
        with self.assertRaises(ValueError):
            s.set_sf(-1)


class TestSerialSessionWipe(unittest.TestCase):
    def test_wipe_eeprom(self):
        mock = MockSerial()
        s = rnode_serial.SerialSession(mock, board=rnode_serial.T1000E)
        s.wipe_eeprom()
        # Expected: FEND 0x59 0xf8 FEND
        self.assertEqual(mock.writes, [bytes([0xC0, 0x59, 0xF8, 0xC0])])

    def test_leave(self):
        mock = MockSerial()
        s = rnode_serial.SerialSession(mock, board=rnode_serial.T1000E)
        s.leave()
        # Expected: FEND 0x0A 0xFF FEND
        self.assertEqual(mock.writes, [bytes([0xC0, 0x0A, 0xFF, 0xC0])])


class TestSerialSessionDetect(unittest.TestCase):
    """Verify the detect() burst of commands hits the wire."""

    def test_detect_writes_expected_frames(self):
        mock = MockSerial()
        s = rnode_serial.SerialSession(mock, board=rnode_serial.T1000E)
        # Inject a fake DETECT response so detect() also exercises the
        # readLoop on the way through.
        mock.inject(_kiss_frame(rnode_serial.KISS.CMD_DETECT, bytes([rnode_serial.KISS.DETECT_RESP])))
        # Inject a fake FW_VERSION response (major=1, minor=86)
        mock.inject(_kiss_frame(rnode_serial.KISS.CMD_FW_VERSION, bytes([1, 86])))
        s.detect()
        # detect() writes all 8 frames in a single .write() call (the
        # session concatenates them onto one frame). Verify the bytes
        # written to the wire contain the right sub-commands.
        all_writes = mock.all_writes_bytes()
        # FENDs framing the requests
        fend = bytes([0xC0])
        # Each command byte should appear with a FEND on either side.
        for cmd in (
            rnode_serial.KISS.CMD_DETECT,
            rnode_serial.KISS.CMD_FW_VERSION,
            rnode_serial.KISS.CMD_PLATFORM,
            rnode_serial.KISS.CMD_MCU,
            rnode_serial.KISS.CMD_BOARD,
            rnode_serial.KISS.CMD_DEV_HASH,
            rnode_serial.KISS.CMD_HASHES,
        ):
            self.assertIn(bytes([0xC0, cmd]), all_writes,
                          f"CMD 0x{cmd:02x} not found on the wire")
        # DETECT_REQ (0x73) should follow the DETECT FEND.
        self.assertIn(bytes([0xC0, rnode_serial.KISS.CMD_DETECT,
                             rnode_serial.KISS.DETECT_REQ, 0xC0]), all_writes)
        # The live fields should have been populated by the readLoop pulse.
        self.assertTrue(s.detected)
        self.assertEqual(s.firmware_version, "1.86")


class TestSerialSessionEEPROM(unittest.TestCase):
    """Verify the CMD_ROM_READ path and parse_eeprom_safe()."""

    def _session_with_dump(self, dump: bytes) -> tuple[MockSerial, rnode_serial.SerialSession]:
        mock = MockSerial()
        # Inject the EEPROM dump framed as a CMD_ROM_READ response.
        mock.inject(_kiss_frame(rnode_serial.KISS.CMD_ROM_READ, dump))
        s = rnode_serial.SerialSession(mock, board=rnode_serial.T1000E)
        return mock, s

    def test_eeprom_read_and_parse(self):
        dump = _makesynthetic_eeprom_dump()
        mock, s = self._session_with_dump(dump)
        result = s.download_eeprom()
        self.assertEqual(result, dump)
        self.assertEqual(s.provisioned, True)
        self.assertEqual(s.checksum_ok, True)
        self.assertEqual(s.product, 0x1E)
        self.assertEqual(s.model, 0xB5)
        self.assertEqual(s.hw_rev, 1)
        self.assertEqual(s.conf_sf, 7)
        self.assertEqual(s.conf_cr, 5)
        self.assertEqual(s.conf_txpower, 14)
        self.assertEqual(s.conf_bandwidth, 125_000)
        self.assertEqual(s.conf_frequency, 915_000_000)
        self.assertTrue(s.conf_ok)

    def test_eeprom_parse_unprovisioned(self):
        dump = _makesynthetic_eeprom_dump(info_locked=False)
        mock, s = self._session_with_dump(dump)
        s.download_eeprom()
        self.assertFalse(s.provisioned)
        # Checksum still matches (the data is just unlocked).
        self.assertTrue(s.checksum_ok)

    def test_eeprom_checksum_mismatch_does_not_exit(self):
        # Build a valid dump, then corrupt the checksum halfway through.
        dump = bytearray(_makesynthetic_eeprom_dump())
        dump[rnode_serial.ROM.ADDR_CHKSUM + 5] ^= 0xFF
        mock, s = self._session_with_dump(bytes(dump))
        # Crucially: this MUST NOT raise. If our wrapper inadvertently calls
        # parse_eeprom() (which hard-exits via graceful_exit), the test would
        # hit SystemExit. We assert it returns and checksum_ok is False.
        s.download_eeprom()
        self.assertFalse(s.checksum_ok)

    def test_eeprom_too_short_raises(self):
        mock = MockSerial()
        mock.inject(_kiss_frame(rnode_serial.KISS.CMD_ROM_READ, b"\x00" * 50))
        s = rnode_serial.SerialSession(mock, board=rnode_serial.T1000E)
        with self.assertRaises(rnode_serial.SerialError):
            s.download_eeprom()


class TestCmdSet(unittest.TestCase):
    """End-to-end test of cmd_set() with a mock serial."""

    def _patch_open_serial(self, mock):
        # Replace the module-level _open_serial so we don't try to import
        # pyserial during this test (it would still succeed but we want
        # explicit control over the device path).
        def _fake_open(dev, baud=115200):
            mock.name = dev
            return mock
        rnode_serial._open_serial = _fake_open

    def test_set_with_yes_skips_interactive_prompt(self):
        mock = MockSerial()
        self._patch_open_serial(mock)
        # Drive cmd_set with --yes so we don't block on stdin.
        args = rnode_serial.build_parser().parse_args(
            ["set", "--dev", "/dev/ttyXXXX", "--freq", "915000000",
             "--bw", "125000", "--sf", "7", "--cr", "5", "--txp", "14", "--yes"]
        )
        rc = rnode_serial.cmd_set(args)
        self.assertEqual(rc, 0)
        # We expect at least 5 frames: the five set_* commands, plus a leave.
        # Each set_* + leave is exercised; we just check that the field
        # values made it onto the wire.
        all_writes = mock.all_writes_bytes()
        # Verify each field's KISS command appeared.
        self.assertIn(bytes([0xC0, 0x01]), all_writes)  # CMD_FREQUENCY
        self.assertIn(bytes([0xC0, 0x02]), all_writes)  # CMD_BANDWIDTH
        self.assertIn(bytes([0xC0, 0x04]), all_writes)  # CMD_SF
        self.assertIn(bytes([0xC0, 0x05]), all_writes)  # CMD_CR
        self.assertIn(bytes([0xC0, 0x03]), all_writes)  # CMD_TXPOWER
        self.assertIn(bytes([0xC0, 0x0A]), all_writes)  # CMD_LEAVE

    def test_set_validates_freq(self):
        mock = MockSerial()
        self._patch_open_serial(mock)
        # freq=100 MHz is out of range for T1000E (863-928 MHz band).
        args = rnode_serial.build_parser().parse_args(
            ["set", "--dev", "/dev/ttyXXXX", "--freq", "100000000", "--yes"]
        )
        with self.assertRaises(SystemExit):
            rnode_serial.cmd_set(args)

    def test_set_validates_sf(self):
        mock = MockSerial()
        self._patch_open_serial(mock)
        # SF=20 is out of range (5-12).
        args = rnode_serial.build_parser().parse_args(
            ["set", "--dev", "/dev/ttyXXXX", "--sf", "20", "--yes"]
        )
        with self.assertRaises(SystemExit):
            rnode_serial.cmd_set(args)

    def test_set_aborts_with_no_fields(self):
        mock = MockSerial()
        self._patch_open_serial(mock)
        args = rnode_serial.build_parser().parse_args(
            ["set", "--dev", "/dev/ttyXXXX", "--yes"]
        )
        with self.assertRaises(SystemExit):
            rnode_serial.cmd_set(args)


class TestCmdWipe(unittest.TestCase):
    def _patch_open_serial(self, mock):
        def _fake_open(dev, baud=115200):
            mock.name = dev
            return mock
        rnode_serial._open_serial = _fake_open

    def test_wipe_with_yes(self):
        mock = MockSerial()
        self._patch_open_serial(mock)
        args = rnode_serial.build_parser().parse_args(
            ["wipe", "--dev", "/dev/ttyXXXX", "--yes"]
        )
        rc = rnode_serial.cmd_wipe(args)
        self.assertEqual(rc, 0)
        self.assertEqual(mock.writes, [bytes([0xC0, 0x59, 0xF8, 0xC0])])

    def test_wipe_requires_explicit_WIPE(self):
        mock = MockSerial()
        self._patch_open_serial(mock)
        args = rnode_serial.build_parser().parse_args(
            ["wipe", "--dev", "/dev/ttyXXXX"]
        )
        # Feed stdin with "yes\n" (lowercase) -- should be rejected.
        saved = sys.stdin
        sys.stdin = io.StringIO("yes\n")
        try:
            rc = rnode_serial.cmd_wipe(args)
        finally:
            sys.stdin = saved
        self.assertEqual(rc, 1)
        # No write should have happened.
        self.assertEqual(mock.writes, [])


class TestCmdShow(unittest.TestCase):
    """Verify cmd_show prints the expected fields without crashing."""

    def _patch_open_serial(self, mock):
        def _fake_open(dev, baud=115200):
            mock.name = dev
            return mock
        rnode_serial._open_serial = _fake_open

    def test_show_full_responses(self):
        mock = MockSerial()
        self._patch_open_serial(mock)

        # We need to inject canned responses *for each call*, because the
        # MockSerial's RX buffer is consumed by the readLoop pulse and
        # there's no way to "replay" data later. Patch SerialSession's
        # detect/query_live_settings/download_eeprom to inject on each call.
        detect_responses = (
            # detect responses
            _kiss_frame(rnode_serial.KISS.CMD_DETECT, bytes([rnode_serial.KISS.DETECT_RESP]))
            + _kiss_frame(rnode_serial.KISS.CMD_FW_VERSION, bytes([1, 86]))
            + _kiss_frame(rnode_serial.KISS.CMD_PLATFORM, bytes([rnode_serial.ROM.PLATFORM_NRF52]))
            + _kiss_frame(rnode_serial.KISS.CMD_MCU, bytes([rnode_serial.ROM.MCU_NRF52]))
            + _kiss_frame(rnode_serial.KISS.CMD_BOARD, bytes([rnode_serial.ROM.BOARD_T1000E]))
            + _kiss_frame(rnode_serial.KISS.CMD_DEV_HASH, b"\xaa" * 32)
            + _kiss_frame(rnode_serial.KISS.CMD_HASHES, bytes([0x01]) + b"\xbb" * 32)
            + _kiss_frame(rnode_serial.KISS.CMD_HASHES, bytes([0x02]) + b"\xcc" * 32)
        )
        query_responses = (
            _kiss_frame(rnode_serial.KISS.CMD_FREQUENCY, (915_000_000).to_bytes(4, "big"))
            + _kiss_frame(rnode_serial.KISS.CMD_BANDWIDTH, (125_000).to_bytes(4, "big"))
            + _kiss_frame(rnode_serial.KISS.CMD_TXPOWER, bytes([14]))
            + _kiss_frame(rnode_serial.KISS.CMD_SF, bytes([7]))
            + _kiss_frame(rnode_serial.KISS.CMD_CR, bytes([5]))
        )
        dump = _makesynthetic_eeprom_dump()
        eeprom_response = _kiss_frame(rnode_serial.KISS.CMD_ROM_READ, dump)

        orig_detect = rnode_serial.SerialSession.detect
        orig_query = rnode_serial.SerialSession.query_live_settings
        orig_dl = rnode_serial.SerialSession.download_eeprom

        def fake_detect(self):
            self.serial.inject(detect_responses)
            orig_detect(self)
        def fake_query(self):
            self.serial.inject(query_responses)
            orig_query(self)
        def fake_dl(self):
            self.serial.inject(eeprom_response)
            return orig_dl(self)

        rnode_serial.SerialSession.detect = fake_detect
        rnode_serial.SerialSession.query_live_settings = fake_query
        rnode_serial.SerialSession.download_eeprom = fake_dl
        self.addCleanup(lambda: setattr(rnode_serial.SerialSession, "detect", orig_detect))
        self.addCleanup(lambda: setattr(rnode_serial.SerialSession, "query_live_settings", orig_query))
        self.addCleanup(lambda: setattr(rnode_serial.SerialSession, "download_eeprom", orig_dl))

        args = rnode_serial.build_parser().parse_args(
            ["show", "--dev", "/dev/ttyXXXX"]
        )
        saved = sys.stdout
        out = io.StringIO()
        sys.stdout = out
        try:
            rc = rnode_serial.cmd_show(args)
        finally:
            sys.stdout = saved
        self.assertEqual(rc, 0)
        text = out.getvalue()
        # Spot-check the printed output.
        self.assertIn("SenseCAP T1000-E", text)
        self.assertIn("0x1e", text.lower())  # product
        self.assertIn("0xb5", text.lower())  # model
        self.assertIn("915000000", text)     # freq
        self.assertIn("125000", text)        # bw
        self.assertIn("Provisioned", text)
        self.assertIn("matches", text)       # checksum

    def test_show_with_no_responses_does_not_crash(self):
        # The device is dead unresponsive. We should still get a clean
        # "(unknown)" report rather than an exception.
        mock = MockSerial()
        self._patch_open_serial(mock)
        args = rnode_serial.build_parser().parse_args(
            ["show", "--dev", "/dev/ttyXXXX"]
        )
        saved = sys.stdout
        saved_err = sys.stderr
        out = io.StringIO()
        err = io.StringIO()
        sys.stdout = out
        sys.stderr = err
        try:
            rc = rnode_serial.cmd_show(args)
        finally:
            sys.stdout = saved
            sys.stderr = saved_err
        self.assertEqual(rc, 0)
        self.assertIn("(unknown)", out.getvalue())
        # The EEPROM read fails and we print a warning to stderr.
        self.assertIn("EEPROM read failed", err.getvalue())


class TestArgparse(unittest.TestCase):
    def test_help_text_contains_subcommands(self):
        text = rnode_serial.build_parser().format_help()
        self.assertIn("show", text)
        self.assertIn("set", text)
        self.assertIn("wipe", text)

    def test_set_accepts_all_field_flags(self):
        for f in rnode_serial.make_fields(rnode_serial.T1000E):
            args = rnode_serial.build_parser().parse_args(
                ["set", "--dev", "/dev/ttyXXXX", f.flag, "1", "--yes"]
            )
            self.assertEqual(getattr(args, f.key), "1")


class TestBoardProfile(unittest.TestCase):
    def test_t1000e_in_board_table(self):
        self.assertIn("t1000e", rnode_serial.BOARDS)
        b = rnode_serial.BOARDS["t1000e"]
        self.assertEqual(b.product, 0x1E)
        self.assertEqual(b.model, 0xB5)
        self.assertEqual(b.board, 0x52)

    def test_unknown_board_errors(self):
        # Replace _resolve_board's internal lookup so we don't have to
        # invoke argparse.
        import argparse
        ns = argparse.Namespace(board="nope")
        with self.assertRaises(SystemExit):
            rnode_serial._resolve_board(ns)


class TestNoRealSerialDevices(unittest.TestCase):
    """Belt-and-braces check: the test suite itself never opens /dev/ttyACM*
    or /dev/ttyUSB*. This is a paranoia test that runs after the others --
    if any earlier test had (incorrectly) tried to open a real device, the
    failure would already be visible; this test just verifies we have the
    guard rails in place."""
    def test_no_serial_dev_paths_in_module(self):
        # The module source must not contain any /dev/ttyACMx or /dev/ttyUSBx
        # string constants that would be auto-opened. Globs in opt arguments
        # are fine (we only *open* what the user passes).
        src = open(os.path.join(HERE, "rnode_serial.py")).read()
        self.assertNotIn('"/dev/ttyACM', src)
        self.assertNotIn('"/dev/ttyUSB', src)
        self.assertNotIn("'/dev/ttyACM", src)
        self.assertNotIn("'/dev/ttyUSB", src)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
