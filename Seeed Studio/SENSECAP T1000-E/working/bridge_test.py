#!/usr/bin/env python3
"""Autonomous RX test via the LoRa<->TCP bridge.

(a) Arms the T1000E and monitors its KISS port for received LoRa packets
    (stat_rx / stat_tx / CMD_DATA), on /dev/ttyACM0.
(b) Brings up an RNS instance with ONLY a TCPClientInterface to the
    Reticulum server at 192.168.223.20:4242, then announces a destination
    repeatedly. The bridge (also on that server) relays announces over LoRa
    (SX1262) -> the T1000E should receive them.

If the T1000E's stat_rx climbs past 1 AND a CMD_DATA frame arrives, the
continuous-RX fix works end to end. Logs to test/bridge_test.log.
"""
import os, sys, time, threading, serial

RUN = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
LOGP = "/home/idan/Downloads/test/bridge_test.log"
logf = open(LOGP, "w")
def L(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True); logf.write(line+"\n"); logf.flush()

# ---------- T1000E KISS monitor (own thread, own serial handle) ----------
FEND,FESC,TFEND,TFESC = 0xC0,0xDB,0xDC,0xDD
def kesc(p):
    o=bytearray()
    for b in p:
        if b==FEND:o+=bytes([FESC,TFEND])
        elif b==FESC:o+=bytes([FESC,TFESC])
        else:o.append(b)
    return bytes(o)
def kfr(c,*d): return bytes([FEND,c])+kesc(bytes(d))+bytes([FEND])
def kunesc(p):
    o,e=bytearray(),False
    for b in p:
        if e:o.append(FEND if b==TFEND else FESC if b==TFESC else b);e=False
        elif b==FESC:e=True
        else:o.append(b)
    return bytes(o)

kiss = {"online":None,"rx":0,"tx":0,"data":0,"run":True}
def kiss_thread():
    try:
        s=serial.Serial("/dev/ttyACM0",115200,timeout=0.05)
    except Exception as e:
        L(f"KISS open failed: {e!r}"); return
    inf,fr=False,bytearray()
    def handle():
        nonlocal fr
        c,pl=fr[0],kunesc(bytes(fr[1:]))
        if c==0x06 and pl: kiss["online"]=pl[0]
        elif c==0x21 and len(pl)>=4: kiss["rx"]=int.from_bytes(pl[:4],"big")
        elif c==0x22 and len(pl)>=4: kiss["tx"]=int.from_bytes(pl[:4],"big")
        elif c==0x00:
            kiss["data"]+=1
            plhex = pl.hex()
            match = None
            for h, seq in kiss.get("announced_hashes", {}).items():
                if h in plhex:
                    match = seq; kiss.setdefault("matched", []).append(seq); break
            tag = f"  <<< MATCHES my announce #{match} (DELIVERY CONFIRMED)" if match is not None else ""
            L(f"*** T1000E RECEIVED+DELIVERED (CMD_DATA, {len(pl)}B): {plhex[:72]}{tag}")
    # arm radio -- force a FRESH begin()/receive() by cycling OFF then ON
    s.write(kfr(0x06,0x00)); s.flush(); time.sleep(0.8)   # radio OFF
    s.write(kfr(0x01,*(917800000).to_bytes(4,"big")))
    s.write(kfr(0x02,*(250000).to_bytes(4,"big")))
    s.write(kfr(0x04,10)); s.write(kfr(0x05,5)); s.write(kfr(0x03,20)); s.flush(); time.sleep(0.3)
    s.write(kfr(0x06,0x01)); s.flush(); time.sleep(1.2)   # radio ON (fresh begin)
    s.write(kfr(0x06,0xFF)); s.flush(); time.sleep(0.3)
    last=time.time()
    while kiss["run"]:
        try: data=s.read(256)
        except Exception as e: L(f"KISS read exc: {e!r}"); return
        for b in data:
            if b==FEND:
                if inf and fr: handle()
                inf,fr=True,bytearray()
            elif inf: fr.append(b)
        if time.time()-last>1.5:
            s.write(kfr(0x21,0x00)); s.write(kfr(0x22,0x00)); s.flush(); last=time.time()
    s.close()

kt=threading.Thread(target=kiss_thread,daemon=True); kt.start()
time.sleep(2.0)
L(f"T1000E armed: radio_online={kiss['online']}  baseline rx={kiss['rx']} data={kiss['data']}")

# ---------- RNS injection over TCP to the bridge ----------
CFGDIR="/home/idan/Downloads/test/rns_inject_config"
os.makedirs(CFGDIR, exist_ok=True)
open(os.path.join(CFGDIR,"config"),"w").write("""[reticulum]
enable_transport = True
share_instance = No
panic_on_interface_error = No

[logging]
loglevel = 3

[interfaces]
  [[TCP To Bridge]]
  type = TCPClientInterface
  interface_enabled = true
  target_host = 192.168.223.20
  target_port = 4242
""")

import RNS
reticulum = RNS.Reticulum(configdir=CFGDIR)
L("RNS up (TCP to 192.168.223.20:4242).")

got_net_announce = {"n":0}
class AH:
    aspect_filter=None
    def received_announce(self, destination_hash, announced_identity, app_data):
        got_net_announce["n"]+=1
        L(f"  (net) saw announce on TCP net: {RNS.prettyhexrep(destination_hash)}")
RNS.Transport.register_announce_handler(AH())

announced_hashes = {}   # short hex -> seq
def check_match(pl_hex):
    for h, seq in announced_hashes.items():
        if h in pl_hex:
            return seq
    return None

# override the KISS CMD_DATA handler path by post-checking in the loop is hard;
# instead, expose announced_hashes to a wrapper that the reader can see.
kiss["announced_hashes"] = announced_hashes
kiss["matched"] = []

L("announcing FRESH destinations every 6s; watching for delivery with matching hash ...")
t0=time.time(); i=0
while time.time()-t0 < RUN:
    ident=RNS.Identity()
    dest=RNS.Destination(ident, RNS.Destination.IN, RNS.Destination.SINGLE, "rxbridgetest", "probe", str(i))
    announced_hashes[dest.hash.hex()] = i
    dest.announce(app_data=f"rxprobe{i}".encode())
    i+=1
    L(f"announce #{i} (fresh {RNS.prettyhexrep(dest.hash)}).  T1000E rx={kiss['rx']} data={kiss['data']} matched={len(kiss['matched'])} netann={got_net_announce['n']}")
    time.sleep(6)

kiss["run"]=False; time.sleep(0.3)
L(f"FINAL: T1000E stat_rx={kiss['rx']} stat_tx={kiss['tx']} data_pkts={kiss['data']} | net announces seen={got_net_announce['n']} | online={kiss['online']}")
