#!/usr/bin/env python3
"""Read the temporary split-RX diagnostic counters from the debug firmware.
Run AFTER stopping the RNS endpoint (port must be free). Counters persist in
firmware until the next reboot, so they reflect the whole session since boot.

  stat_rx       : good-CRC RX_DONE events  (every LoRa packet, incl. each split part)
  stat_tx       : CRC-error RX_DONE events
  stat_rxbig    : good-CRC RX_DONE with pld_len >= 250  (full-size LoRa packets!)
  stat_lastrxlen: pld_len of the most recent good packet
"""
import serial, sys, time
PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_STAT_RX, CMD_STAT_TX, CMD_STAT_DBG = 0x21, 0x22, 0x63
vals = {}

def unesc(p):
    out, e = bytearray(), False
    for b in p:
        if e: out.append(FEND if b==TFEND else FESC if b==TFESC else b); e=False
        elif b==FESC: e=True
        else: out.append(b)
    return bytes(out)

s = serial.Serial(PORT, 115200, timeout=0.1); time.sleep(0.4)
# warm/drain
for _ in range(10): s.write(bytes([FEND,0x08,0x73,FEND])); s.flush(); s.read(256); time.sleep(0.05)

def collect(seconds=2.0):
    inf, fr = False, bytearray(); t=time.time()
    while time.time()-t < seconds:
        for b in s.read(256):
            if b==FEND:
                if inf and fr:
                    cmd=fr[0]; pl=unesc(bytes(fr[1:]))
                    if cmd==CMD_STAT_RX and len(pl)>=4: vals['stat_rx']=int.from_bytes(pl[:4],'big')
                    elif cmd==CMD_STAT_TX and len(pl)>=4: vals['stat_tx']=int.from_bytes(pl[:4],'big')
                    elif cmd==CMD_STAT_DBG and len(pl)>=6:
                        vals['stat_rxbig']=int.from_bytes(pl[:4],'big')
                        vals['stat_lastrxlen']=int.from_bytes(pl[4:6],'big')
                inf, fr = True, bytearray()
            elif inf: fr.append(b)

for _ in range(6):
    s.write(bytes([FEND,CMD_STAT_RX,FEND])); s.write(bytes([FEND,CMD_STAT_TX,FEND])); s.write(bytes([FEND,CMD_STAT_DBG,FEND]))
    s.flush(); collect(0.5)
    if {'stat_rx','stat_tx','stat_rxbig'} <= set(vals): break
s.close()
print("=== T1000E split-RX diagnostics ===")
for k in ('stat_rx','stat_tx','stat_rxbig','stat_lastrxlen'):
    print("  %-14s = %s" % (k, vals.get(k, 'NO RESPONSE')))
