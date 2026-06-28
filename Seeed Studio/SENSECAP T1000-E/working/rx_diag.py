#!/usr/bin/env python3
"""Bring the T1000E radio online via KISS, then live-monitor the RX
diagnostic counters while announces are spammed from the Heltec.

Diagnostic firmware repurposes:
  stat_rx = count of RX_DONE IRQ fires (any, incl. CRC errors)
  stat_tx = count of those that had a CRC error

A dedicated reader thread drains the port continuously (mandatory, see
AGENTS.md backpressure lesson) and parses every KISS frame.
"""
import serial, threading, time, sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
DUR  = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_FREQUENCY, CMD_BANDWIDTH, CMD_TXPOWER = 0x01, 0x02, 0x03
CMD_SF, CMD_CR, CMD_RADIO_STATE, CMD_RADIO_LOCK = 0x04, 0x05, 0x06, 0x07
CMD_STAT_RX, CMD_STAT_TX, CMD_ERROR = 0x21, 0x22, 0x90

# From ~/.reticulum/config [[AA6B]]: 917.8 MHz / 250 kHz / SF10 / CR5 / 20 dBm
FREQ, BW, SF, CR, TXP = 917_800_000, 250_000, 10, 5, 20

ser = serial.Serial(PORT, 115200, timeout=0.05)
state = {"online": None, "errors": [], CMD_STAT_RX: None, CMD_STAT_TX: None}
running = True

def esc(payload):
    out = bytearray()
    for b in payload:
        if b == FEND: out += bytes([FESC, TFEND])
        elif b == FESC: out += bytes([FESC, TFESC])
        else: out.append(b)
    return bytes(out)

def unesc(payload):
    out, e = bytearray(), False
    for b in payload:
        if e:
            out.append(FEND if b == TFEND else FESC if b == TFESC else b); e = False
        elif b == FESC: e = True
        else: out.append(b)
    return bytes(out)

def send(cmd, payload=b""):
    ser.write(bytes([FEND, cmd]) + esc(payload) + bytes([FEND])); ser.flush()

def reader():
    in_frame, frame = False, bytearray()
    while running:
        data = ser.read(256)
        for b in data:
            if b == FEND:
                if in_frame and frame:
                    cmd, pl = frame[0], unesc(bytes(frame[1:]))
                    if cmd in (CMD_STAT_RX, CMD_STAT_TX) and len(pl) >= 4:
                        state[cmd] = int.from_bytes(pl[:4], "big")
                    elif cmd == CMD_RADIO_STATE and pl:
                        state["online"] = pl[0]
                    elif cmd == CMD_ERROR and pl:
                        state["errors"].append(pl[0])
                    elif cmd == CMD_FREQUENCY and len(pl) >= 4:
                        state["freq"] = int.from_bytes(pl[:4], "big")
                    elif cmd == CMD_BANDWIDTH and len(pl) >= 4:
                        state["bw"] = int.from_bytes(pl[:4], "big")
                    elif cmd == CMD_SF and pl:
                        state["sf"] = pl[0]
                    elif cmd == CMD_CR and pl:
                        state["cr"] = pl[0]
                    elif cmd == CMD_TXPOWER and pl:
                        state["txp"] = pl[0]
                    elif cmd == CMD_RADIO_LOCK and pl:
                        state["locked"] = pl[0]
                in_frame, frame = True, bytearray()
            elif in_frame:
                frame.append(b)

threading.Thread(target=reader, daemon=True).start()
time.sleep(0.3)

print("Configuring radio: 917.8MHz / 250kHz / SF10 / CR5 / 20dBm ...")
send(CMD_FREQUENCY, FREQ.to_bytes(4, "big"))
send(CMD_BANDWIDTH, BW.to_bytes(4, "big"))
send(CMD_SF, bytes([SF]))
send(CMD_CR, bytes([CR]))
send(CMD_TXPOWER, bytes([TXP]))
time.sleep(0.3)
# Read back config + radio lock to confirm everything landed
send(CMD_FREQUENCY, (0).to_bytes(4, "big"))   # freq=0 -> query
send(CMD_BANDWIDTH, (0).to_bytes(4, "big"))   # bw=0   -> query
send(CMD_SF, bytes([0xFF]))
send(CMD_CR, bytes([0xFF]))
send(CMD_TXPOWER, bytes([0xFF]))
send(CMD_RADIO_LOCK, bytes([0x00]))           # update_radio_lock + report
time.sleep(0.6)
print(f"  readback: freq={state.get('freq')} bw={state.get('bw')} "
      f"sf={state.get('sf')} cr={state.get('cr')} txp={state.get('txp')} "
      f"radio_locked={state.get('locked')}")
print("Turning radio ON (CMD_RADIO_STATE=1) ... (begin() can block ~3s)")
send(CMD_RADIO_STATE, bytes([0x01]))
# begin() blocks on version poll + calibrate + LED info pattern; poll state
for _ in range(20):
    time.sleep(0.3)
    send(CMD_RADIO_STATE, bytes([0xFF]))  # query current state
    if state["online"] is not None:
        break

if state["errors"]:
    names = {1: "INITRADIO", 2: "TXFAILED", 6: "MODEM_TIMEOUT"}
    print("RADIO ERROR(S):", [names.get(e, hex(e)) for e in state["errors"]])
print(f"radio_online = {state['online']}  (1 = armed for RX, 0 = OFF)")
if state["online"] != 1:
    print(">>> Radio did NOT come online. RX cannot work until this is fixed.")
    print(">>> Likely hw_ready=false (firmware-hash gate) or radio_locked.")

print(f"\nMonitoring RX counters for {DUR:.0f}s -- SPAM ANNOUNCES FROM THE HELTEC NOW.\n")
deadline = time.time() + DUR
last = None
while time.time() < deadline:
    send(CMD_STAT_RX); time.sleep(0.05)
    send(CMD_STAT_TX); time.sleep(0.05)
    cur = (state[CMD_STAT_RX], state[CMD_STAT_TX])
    if cur != last:
        print(f"  t+{DUR-(deadline-time.time()):5.1f}s  RX_DONE={cur[0]}  CRC_err={cur[1]}")
        last = cur
    time.sleep(0.4)

running = False
time.sleep(0.2)
print(f"\nFINAL: stat_rx (RX_DONE fires) = {state[CMD_STAT_RX]}   "
      f"stat_tx (CRC errors) = {state[CMD_STAT_TX]}")
ser.close()
