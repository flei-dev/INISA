#!/usr/bin/env python3
"""
storage_server_v3.py - 存储节点（干净版）
"""
import sys, os, time, random, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scapy.all import *
from scapy.all import conf
from hosts.tdtp_packet import *

IFACE       = sys.argv[1]        if len(sys.argv) > 1 else 'eth0'
BASE_LAT_MS = float(sys.argv[2]) if len(sys.argv) > 2 else 50.0
JITTER_MS   = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
LOG_FILE    = sys.argv[4]        if len(sys.argv) > 4 else '/tmp/store.log'
CONG_FILE   = sys.argv[5]        if len(sys.argv) > 5 else ''

l2_sock = conf.L2socket(iface=IFACE)
req_count = 0

def get_latency():
    lat = max(1.0, BASE_LAT_MS + random.gauss(0, JITTER_MS))
    if CONG_FILE and os.path.exists(CONG_FILE):
        try:
            with open(CONG_FILE) as f:
                factor = float(f.read().strip())
            lat *= factor
        except: pass
    return lat

def delayed_send(resp, delay_ms):
    time.sleep(delay_ms / 1000.0)
    try:
        l2_sock.send(resp)
    except: pass

def handle(pkt):
    global req_count
    if not pkt.haslayer(UDP):
        return
    try:
        payload = bytes(pkt[UDP].payload)
        if not payload or len(payload) < 10:
            return
        tdtp = TDTP(payload)
        if tdtp.opcode not in (TDTP_READ, TDTP_PREFETCH):
            return

        req_count += 1
        lat = get_latency()

        resp = (Ether(src=pkt[Ether].dst, dst='ff:ff:ff:ff:ff:ff') /
                IP(src=pkt[IP].dst, dst='10.0.1.1', ttl=64) /
                UDP(sport=TDTP_PORT, dport=TDTP_PORT) /
                TDTP(version=1,
                     opcode=TDTP_ACK,
                     taskId=tdtp.taskId,
                     batchId=tdtp.batchId,
                     objectId=tdtp.objectId,
                     seqNum=tdtp.seqNum,
                     offset=tdtp.offset,
                     length=tdtp.length,
                     flags=0))

        threading.Thread(target=delayed_send, args=(resp, lat), daemon=True).start()

        if req_count % 20 == 0:
            op = "READ" if tdtp.opcode == TDTP_READ else "PREFETCH"
            print(f"[Store] #{req_count} {op} task={tdtp.taskId} "
                  f"batch={tdtp.batchId} lat={lat:.0f}ms", flush=True)

    except Exception as e:
        print(f"[Store] ERROR: {e}", flush=True)

if __name__ == '__main__':
    print(f"[Store] Started: iface={IFACE}, lat={BASE_LAT_MS}±{JITTER_MS}ms", flush=True)
    sniff(iface=IFACE, filter="udp", prn=handle)