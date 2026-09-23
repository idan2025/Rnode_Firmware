#!/usr/bin/env python3
"""rnodeconf_t1000e.py - run the stock rnodeconf with Seeed SenseCAP T1000-E support.

Upstream rnodeconf (pip package ``rns``) does not know the T1000-E yet, and
crashes on it with ``KeyError: 181`` (model 0xB5). This wrapper adds the
T1000-E product/model/board IDs to the installed rnodeconf at runtime and
points firmware downloads at this repository's releases, then hands every
argument to the unmodified rnodeconf ``main()``.

Usage (same arguments as rnodeconf):

  pip install rns adafruit-nrfutil
  python tools/rnodeconf_t1000e.py -i /dev/ttyACM0                 # device info
  python tools/rnodeconf_t1000e.py -r --product 1e --model b5 --hwrev 1 /dev/ttyACM0
  python tools/rnodeconf_t1000e.py -u /dev/ttyACM0                 # update to latest release

Firmware is fetched from https://github.com/idan2025/Rnode_Firmware/releases
(pass --fw-url to override). Each release carries a ``release.json`` with the
zip's SHA-256, which rnodeconf verifies before flashing. ``--fw-version`` does
not work with these releases (rnodeconf requires a numeric version and the
tags are named like ``v1.7-t1000e``), so ``-u`` always installs the latest.

rnodeconf flashes nRF52 boards with ``adafruit-nrfutil ... -t 1200``, which
does the 1200-baud bootloader touch itself but does not wait long enough for
the T1000-E bootloader to enumerate. The wrapper does the touch itself, waits
for the bootloader port (USB 2886:0057), and then runs nrfutil on it.

This wrapper is only meant for the T1000-E. For other boards use the stock
rnodeconf, which downloads from the official RNode firmware releases.
"""

import subprocess
import sys
import time

FW_URL = "https://github.com/idan2025/Rnode_Firmware/releases/"

PRODUCT_T1000E = 0x1E
MODEL_B5       = 0xB5
BOARD_T1000E   = 0x52

SEEED_VID       = 0x2886
BOOTLOADER_PID  = 0x0057
BOOTLOADER_WAIT = 30


def patch(rnodeconf):
    rom = rnodeconf.ROM
    rom.PRODUCT_T1000E = PRODUCT_T1000E
    rom.MODEL_B5       = MODEL_B5
    rom.BOARD_T1000E   = BOARD_T1000E

    rnodeconf.products.setdefault(PRODUCT_T1000E, "Seeed SenseCAP T1000-E")
    rnodeconf.models.setdefault(
        MODEL_B5, [863000000, 928000000, 22, "863 - 928 MHz", "rnode_firmware_t1000e.zip", "LR1110"])


def find_bootloader_port():
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.vid == SEEED_VID and p.pid == BOOTLOADER_PID:
            return p.device
    return None


def touch_into_bootloader(port):
    import serial
    s = serial.Serial(port, 1200)
    time.sleep(0.3)
    s.close()
    deadline = time.time() + BOOTLOADER_WAIT
    while time.time() < deadline:
        bootloader_port = find_bootloader_port()
        if bootloader_port:
            time.sleep(1)
            return bootloader_port
        time.sleep(0.5)
    return None


def patch_dfu_call():
    stock_call = subprocess.call

    def call(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "adafruit-nrfutil" and "-t" in cmd and "-p" in cmd:
            cmd = list(cmd)
            t = cmd.index("-t"); del cmd[t:t+2]
            p = cmd.index("-p")
            print("Resetting device into bootloader...")
            bootloader_port = touch_into_bootloader(cmd[p+1])
            if bootloader_port is None:
                print("Bootloader did not appear within "+str(BOOTLOADER_WAIT)+" seconds, cannot flash.")
                return 1
            cmd[p+1] = bootloader_port
            cmd.append("--singlebank")
        return stock_call(cmd, *args, **kwargs)

    subprocess.call = call


def with_default_fw_url(argv):
    if any(a == "--fw-url" or a.startswith("--fw-url=") for a in argv):
        return argv
    return argv[:1] + ["--fw-url", FW_URL] + argv[1:]


def main():
    try:
        import RNS.Utilities.rnodeconf as rnodeconf
    except ImportError:
        print("rnodeconf not found. Install it with: pip install rns adafruit-nrfutil")
        return 1

    patch(rnodeconf)
    patch_dfu_call()
    sys.argv = with_default_fw_url(sys.argv)
    return rnodeconf.main()


if __name__ == "__main__":
    sys.exit(main())
