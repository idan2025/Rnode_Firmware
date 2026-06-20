#!/usr/bin/env python3
"""Passive RX monitor: radio is already armed (radio_online=1). Polls the
stat_rx/stat_tx diagnostic counters and also captures any CMD_DATA (0x00)
frames -- i.e. actually-received LoRa packets -- while the Heltec transmits.

Writes a timestamped log to test/rx_monitor.log. Does NOT reconfigure or
re-arm the radio (so it won't disturb the armed state).

  stat_rx = RX_DONE IRQ fires (incl. CRC errors)
  stat_tx = CRC errors among those
"""
import serial, threading, time, sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
DUR  = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
LOG  = "/home/idan/Downloads/test/rx_monitor.log"

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
log = open(LOG, "w")
def L(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    log.write(line + "\n"); log.flush()

st = {"online": None, "rx": 0, "tx": 0, "data_pkts": 0}
running = True

def unesc(p):
    o, e = bytearray(), False
    for b in p:
        if e: o.append(FEND if b==TFEND else FESC if b==TFESC else b); e=False
        elif b==FESC: e=True
        else: o.append(b)
    return bytes(o)

def reader():
    inf, fr = False, bytearray()
    while running:
        try: data = ser.read(256)
        except Exception as e: L(f"READ EXC (reboot?): {e!r}"); return
        for b in data:
            if b == FEND:
                if inf and fr:
                    c, pl = fr[0], unesc(bytes(fr[1:]))
                    if c == 0x06 and pl: st["online"] = pl[0]
                    elif c == 0x21 and len(pl) >= 4: st["rx"] = int.from_bytes(pl[:4],"big")
                    elif c == 0x22 and len(pl) >= 4: st["tx"] = int.from_bytes(pl[:4],"big")
                    elif c == 0x00:  # CMD_DATA -- a received packet!
                        st["data_pkts"] += 1
                        L(f"*** CMD_DATA received packet ({len(pl)} bytes): {pl.hex()[:80]}")
                inf, fr = True, bytearray()
            elif inf: fr.append(b)

ser = serial.Serial(PORT, 115200, timeout=0.05)
threading.Thread(target=reader, daemon=True).start()
time.sleep(0.3)
ser.write(bytes([FEND,0x06,0xFF,FEND])); ser.flush(); time.sleep(0.4)
L(f"start: radio_online={st['online']}  (1=armed)")
L("MONITORING -- transmit announces from the Heltec now.")

last = None
deadline = time.time() + DUR
while time.time() < deadline:
    ser.write(bytes([FEND,0x21,0x00,FEND]))
    ser.write(bytes([FEND,0x22,0x00,FEND]))
    ser.flush()
    cur = (st["rx"], st["tx"], st["data_pkts"])
    if cur != last:
        L(f"stat_rx(RX_DONE)={cur[0]}  stat_tx(CRCerr)={cur[1]}  data_pkts={cur[2]}")
        last = cur
    time.sleep(0.5)

running = False
time.sleep(0.2)
L(f"DONE. final stat_rx={st['rx']} stat_tx={st['tx']} data_pkts={st['data_pkts']} online={st['online']}")
ser.close()
