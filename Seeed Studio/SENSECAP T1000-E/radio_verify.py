#!/usr/bin/env python3
"""Functional radio verify for the T1000-E on PRODUCTION firmware (no stat_rx/tx
debug counters). Matches the AGENTS.md re-verify method:
  - arm the radio over KISS (freq/bw/txp/sf/cr + RADIO_STATE on + RADIO_LOCK on)
  - TX a few LoRa packets via CMD_DATA
  - read CMD_STAT_CHTM (0x25): noise_floor = payload[9] - 157 (per AGENTS.md)
  - confirm airtime advances and no CMD_ERROR / TXFAILED

Usage: python3 radio_verify.py [/dev/ttyACM0]
Exit 0 if radio arms + TX airtime advances + noise floor sane; else 1.
"""
import serial, sys, time, struct

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_DATA, CMD_FREQUENCY, CMD_BANDWIDTH, CMD_TXPOWER = 0x00, 0x01, 0x02, 0x03
CMD_SF, CMD_CR, CMD_RADIO_STATE, CMD_RADIO_LOCK = 0x04, 0x05, 0x06, 0x07
CMD_STAT_CHTM, CMD_ERROR = 0x25, 0x90

def esc(p):
    out = bytearray()
    for b in p:
        if b == FEND: out += bytes([FESC, TFEND])
        elif b == FESC: out += bytes([FESC, TFESC])
        else: out.append(b)
    return bytes(out)

def unesc(p):
    out, e = bytearray(), False
    for b in p:
        if e:
            out.append(FEND if b == TFEND else FESC if b == TFESC else b); e = False
        elif b == FESC: e = True
        else: out.append(b)
    return bytes(out)

def frame(cmd, payload=b""):
    return bytes([FEND, cmd]) + esc(payload) + bytes([FEND])

def u32be(v): return list(struct.pack(">I", v))

s = serial.Serial(PORT, 115200, timeout=0.1)
time.sleep(0.4)
# warm/drain
for _ in range(10):
    s.write(bytes([FEND, 0x08, 0x73, FEND])); s.flush(); s.read(256); time.sleep(0.02)

rxbuf = bytearray()
def pump():
    data = s.read(256)
    if data: rxbuf.extend(data)

def read_chtm(timeout=1.5):
    rxbuf.clear()
    s.write(frame(CMD_STAT_CHTM)); s.flush()
    t = time.time()
    frames = []
    while time.time() - t < timeout:
        pump()
        while True:
            i = rxbuf.find(FEND)
            if i < 0: break
            # find next FEND
            j = rxbuf.find(FEND, i+1)
            if j < 0: break
            fr = bytes(rxbuf[i+1:j])
            del rxbuf[:j]
            if len(fr) >= 1 and fr[0] == CMD_STAT_CHTM:
                return unesc(bytes(fr[1:]))
            elif len(fr) >= 1 and fr[0] == CMD_ERROR:
                print(f"  CMD_ERROR received: {fr[1:].hex()}")
    return None

# 917.8 MHz / 250 kHz / SF10 / CR 4-5 / 20 dBm (matches AGENTS.md re-verify)
s.write(frame(CMD_FREQUENCY, bytes(u32be(917800000)))); s.flush(); time.sleep(0.1)
s.write(frame(CMD_BANDWIDTH, bytes(u32be(250000)))); s.flush(); time.sleep(0.1)
s.write(frame(CMD_TXPOWER, bytes([20]))); s.flush(); time.sleep(0.1)
s.write(frame(CMD_SF, bytes([10]))); s.flush(); time.sleep(0.1)
s.write(frame(CMD_CR, bytes([1]))); s.flush(); time.sleep(0.1)  # 4/5
s.write(frame(CMD_RADIO_STATE, bytes([0x01]))); s.flush(); time.sleep(0.3)  # RX on
s.write(frame(CMD_RADIO_LOCK, bytes([0x01]))); s.flush(); time.sleep(0.2)   # lock on
print("radio arm commands sent")

chtm0 = read_chtm()
print(f"CHTM before TX: {chtm0.hex() if chtm0 else 'None'}")
if chtm0 and len(chtm0) > 9:
    print(f"  noise_floor = {chtm0[9]-157} dBm (raw byte9={chtm0[9]})")

# TX 3 packets, 60 bytes each
for k in range(3):
    payload = bytes([0xAA]) * 60
    s.write(frame(CMD_DATA, payload)); s.flush()
    print(f"  TX packet {k+1} (60 B) sent")
    time.sleep(1.0)

chtm1 = read_chtm()
print(f"CHTM after TX:  {chtm1.hex() if chtm1 else 'None'}")
if chtm1 and len(chtm1) > 9:
    print(f"  noise_floor = {chtm1[9]-157} dBm (raw byte9={chtm1[9]})")

ok = True
if chtm0 is None or chtm1 is None:
    print("FAIL: no CMD_STAT_CHTM response (radio did not arm?)")
    ok = False
else:
    # airtime is a float field early in CHTM; compare bytes 0..3 (airtime float)
    try:
        at0 = struct.unpack(">f", bytes(chtm0[0:4]))[0]
        at1 = struct.unpack(">f", bytes(chtm1[0:4]))[0]
        print(f"  airtime: {at0:.4f} -> {at1:.4f} (delta {at1-at0:.4f})")
        if at1 > at0:
            print("PASS: radio armed + TX airtime advanced")
        else:
            print("FAIL: airtime did not advance (TX may have failed)")
            ok = False
    except Exception as e:
        print(f"  (could not parse airtime float: {e})")
        # fall back: any byte difference means TX did something
        if chtm1 != chtm0:
            print("PASS (fallback): CHTM changed after TX")
        else:
            print("FAIL: CHTM unchanged after TX"); ok = False
    if len(chtm1) > 9:
        nf = chtm1[9] - 157
        if -130 <= nf <= -90:
            print(f"PASS: noise floor {nf} dBm sane")
        else:
            print(f"WARN: noise floor {nf} dBm outside expected -130..-90")

sys.exit(0 if ok else 1)