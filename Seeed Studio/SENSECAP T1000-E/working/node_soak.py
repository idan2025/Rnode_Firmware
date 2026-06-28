#!/usr/bin/env python3
"""One RNS node for the bidirectional soak test. Two instances run together,
talking THROUGH the user's LoRa<->TCP bridge:

  node_soak.py lora  -> RNS over the T1000E RNodeInterface (device under test)
  node_soak.py tcp   -> RNS over TCPClientInterface to 192.168.223.20:4242

Each cycle each node announces a FRESH destination (new identity) tagged
"SOAK:<ME>:<seq>" and logs every SOAK announce it hears FROM THE PEER. Fresh
destinations defeat Reticulum's announce-suppression so traffic keeps flowing,
which is what makes this a real soak.

  - tcp node hears LORA announces  => T1000E TX works (LoRa->bridge->TCP)
  - lora node hears TCP announces  => T1000E RX works (TCP->bridge->LoRa)
  - both, sustained for the whole run => bidirectional + stable over time
"""
import os, sys, time

ROLE = sys.argv[1]
DUR  = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
CYCLE = 10.0
MYNAME   = "LORA" if ROLE == "lora" else "TCP"
PEERNAME = "TCP"  if ROLE == "lora" else "LORA"
APP = "soaktest"

LOGP = f"/home/idan/Downloads/test/soak_{ROLE}.log"
logf = open(LOGP, "w")
def L(m):
    line = f"[{time.strftime('%H:%M:%S')}] {MYNAME}: {m}"
    logf.write(line+"\n"); logf.flush(); print(line, flush=True)

CFGDIR = f"/home/idan/Downloads/test/rns_cfg_{ROLE}"
os.makedirs(CFGDIR, exist_ok=True)
iface = ("""  [[T1000E]]
  type = RNodeInterface
  interface_enabled = true
  port = /dev/ttyACM0
  frequency = 917800000
  bandwidth = 250000
  txpower = 20
  spreadingfactor = 10
  codingrate = 5
""" if ROLE == "lora" else """  [[TCP To Bridge]]
  type = TCPClientInterface
  interface_enabled = true
  target_host = 192.168.223.20
  target_port = 4242
""")
open(os.path.join(CFGDIR,"config"),"w").write(
f"""[reticulum]
enable_transport = True
share_instance = No
panic_on_interface_error = No
[logging]
loglevel = 3
[interfaces]
{iface}""")

import RNS
stats = {"heard":0, "first":None, "last":None, "seqs":[], "pings_rx":0, "pings_tx":0}
peer = {"ident":None}

def packet_cb(data, packet):
    stats["pings_rx"] += 1
    try: txt = data.decode(errors="replace")
    except Exception: txt = repr(data)
    L(f"*** PING RECEIVED #{stats['pings_rx']} (RX of addressed data CONFIRMED): {txt}")

class AH:
    aspect_filter = None
    def received_announce(self, destination_hash, announced_identity, app_data):
        if not app_data or not app_data.startswith(b"SOAK:"): return
        parts = app_data.decode(errors="replace").split(":")
        who = parts[1] if len(parts) > 1 else "?"
        if who != PEERNAME: return
        seq = parts[2] if len(parts) > 2 else "?"
        stats["heard"] += 1
        now = time.time()
        if stats["first"] is None: stats["first"] = now
        stats["last"] = now
        stats["seqs"].append(seq)
        peer["ident"] = announced_identity
        L(f"HEARD {PEERNAME} #{seq}  (total from peer={stats['heard']})")

reticulum = RNS.Reticulum(configdir=CFGDIR)
ident = RNS.Identity()
my_dest = RNS.Destination(ident, RNS.Destination.IN, RNS.Destination.SINGLE, APP, MYNAME)
my_dest.set_packet_callback(packet_cb)
my_dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
RNS.Transport.register_announce_handler(AH())
L(f"up. role={ROLE} dur={DUR:.0f}s peer={PEERNAME} my_dest={RNS.prettyhexrep(my_dest.hash)}")

t0 = time.time(); seq = 0; last_ann = 0
while time.time() - t0 < DUR:
    now = time.time()
    # announce every ~30s for discovery
    if now - last_ann >= 30:
        seq += 1; last_ann = now
        my_dest.announce(app_data=f"SOAK:{MYNAME}:{seq}".encode())
    # once the peer is known, ping it EVERY cycle (continuous addressed traffic = the soak)
    if peer["ident"] is not None:
        try:
            out = RNS.Destination(peer["ident"], RNS.Destination.OUT, RNS.Destination.SINGLE, APP, PEERNAME)
            stats["pings_tx"] += 1
            RNS.Packet(out, f"ping from {MYNAME} #{stats['pings_tx']} @{time.strftime('%H:%M:%S')}".encode()).send()
        except Exception as e:
            L(f"ping send failed: {e!r}")
    L(f"[t+{now-t0:4.0f}s] heard={stats['heard']} pings_tx={stats['pings_tx']} pings_rx={stats['pings_rx']}")
    time.sleep(CYCLE)

dur_heard = (stats["last"]-stats["first"]) if stats["first"] else 0
L(f"DONE. announced={seq} heard_from_peer={stats['heard']} pings_tx={stats['pings_tx']} pings_rx={stats['pings_rx']} span={dur_heard:.0f}s")
