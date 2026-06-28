#!/usr/bin/env python3
"""Re-sync the T1000E on-device firmware-hash gate WITHOUT reflashing.

device_init() (Device.h) only sets hw_ready=true if the live SHA256 of the
running application flash matches the target hash stored in EEPROM. A direct
adafruit-nrfutil flash does NOT update that target, so after such a flash
hw_ready stays false and the radio silently never arms (TX poll + serial
still work, masking it).

This reads the stored target (CMD_HASHES/0x01) and the device's live
self-computed hash (CMD_HASHES/0x02). If they differ, it writes the live
hash back as the new target (CMD_FW_HASH/0x58), which the firmware saves to
EEPROM and then hard-resets to apply.
"""
import serial, threading, time, sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_HASHES, CMD_FW_HASH = 0x60, 0x58

ser = serial.Serial(PORT, 115200, timeout=0.05)
hashes = {0x01: None, 0x02: None}
running = True

def unesc(p):
    out, e = bytearray(), False
    for b in p:
        if e: out.append(FEND if b==TFEND else FESC if b==TFESC else b); e=False
        elif b==FESC: e=True
        else: out.append(b)
    return bytes(out)

def esc(p):
    out = bytearray()
    for b in p:
        if b==FEND: out += bytes([FESC,TFEND])
        elif b==FESC: out += bytes([FESC,TFESC])
        else: out.append(b)
    return bytes(out)

def reader():
    inf, fr = False, bytearray()
    while running:
        for b in ser.read(256):
            if b==FEND:
                if inf and fr:
                    if fr[0]==CMD_HASHES and len(fr)>=2:
                        pl = unesc(bytes(fr[2:]))
                        if len(pl)>=32: hashes[fr[1]] = pl[:32]
                inf, fr = True, bytearray()
            elif inf: fr.append(b)

threading.Thread(target=reader, daemon=True).start()
time.sleep(0.3)

for _ in range(10):
    ser.write(bytes([FEND, CMD_HASHES, 0x01, FEND]))  # target
    ser.write(bytes([FEND, CMD_HASHES, 0x02, FEND]))  # live
    ser.flush(); time.sleep(0.3)
    if hashes[0x01] is not None and hashes[0x02] is not None:
        break

tgt, live = hashes[0x01], hashes[0x02]
print("stored target hash :", tgt.hex() if tgt else None)
print("live firmware hash :", live.hex() if live else None)

if live is None:
    print("ERROR: device did not report its live hash; aborting."); running=False; ser.close(); sys.exit(1)

if tgt == live:
    print("\nMATCH -- hash gate is already in sync. hw_ready is NOT blocked by the hash.")
    print("If the radio still won't arm, suspect bt_ready (Bluefruit.begin) instead.")
    running=False; ser.close(); sys.exit(0)

print("\nMISMATCH -- this is why hw_ready=false and the radio never arms.")
if "--write" in sys.argv:
    print("Writing live hash as new target (device will save + hard-reset)...")
    ser.write(bytes([FEND, CMD_FW_HASH]) + esc(live) + bytes([FEND]))
    ser.flush(); time.sleep(1.0)
    print("Sent. Device should reboot/re-enumerate now. Re-check after it reappears.")
else:
    print("Re-run with --write to sync it.")

running=False; ser.close()
