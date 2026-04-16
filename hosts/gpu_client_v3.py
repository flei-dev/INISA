#!/usr/bin/env python3
"""
gpu_client_v3.py - 参数化 GPU 训练节点
=======================================
GPU 只上报自己实际经历的 comp_time 和 load_time。
真实存储延迟由控制平面独立测量 PREFETCH RTT 获得。
"""
import sys, os, time, random, csv, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scapy.all import *
from hosts.tdtp_packet import *

TASK_ID        = int(sys.argv[1])   if len(sys.argv) > 1 else 1
IFACE          = sys.argv[2]        if len(sys.argv) > 2 else 'eth0'
TOTAL_BATCHES  = int(sys.argv[3])   if len(sys.argv) > 3 else 100
COMP_MEAN      = float(sys.argv[4]) if len(sys.argv) > 4 else 200.0
COMP_STD       = float(sys.argv[5]) if len(sys.argv) > 5 else 15.0
STOR_LATENCY   = float(sys.argv[6]) if len(sys.argv) > 6 else 50.0
MODE           = sys.argv[7]        if len(sys.argv) > 7 else 'baseline'
OUTPUT_CSV     = sys.argv[8]        if len(sys.argv) > 8 else '/tmp/gpu_result.csv'

prefetch_arrived = {}
prefetch_lock = threading.Lock()
stop_sniffer = threading.Event()

def ack_sniffer():
    """后台线程：监听 ACK 报文，用循环短超时避免 Scapy 长时间 sniff 挂死"""
    def handle(pkt):
        if stop_sniffer.is_set():
            return
        try:
            payload = bytes(pkt[UDP].payload) if pkt.haslayer(UDP) else None
            if payload:
                tdtp = TDTP(payload)
                if tdtp.opcode == TDTP_ACK:
                    with prefetch_lock:
                        if tdtp.batchId not in prefetch_arrived:
                            prefetch_arrived[tdtp.batchId] = time.time()
        except Exception as e:
            print(f"[GPU-{TASK_ID}] sniffer error: {e}")

    # ★ 循环短超时，每10秒重启一次sniff，避免Scapy长时间运行挂死
    while not stop_sniffer.is_set():
        try:
            sniff(iface=IFACE, filter='udp', prn=handle,
                  stop_filter=lambda _: stop_sniffer.is_set(),
                  timeout=10)
        except Exception as e:
            print(f"[GPU-{TASK_ID}] sniffer restart: {e}")
            time.sleep(0.1)

def simulate_training():
    print(f'[GPU-{TASK_ID}] Starting: {TOTAL_BATCHES} batches, mode={MODE}, '
          f'comp={COMP_MEAN}±{COMP_STD}ms, stor_lat={STOR_LATENCY}ms', flush=True)

    sniffer_thread = threading.Thread(target=ack_sniffer, daemon=True)
    sniffer_thread.start()
    time.sleep(0.5)

    records = []
    rng = random.Random(42 + TASK_ID)

    for batch_id in range(TOTAL_BATCHES):
        comp_time = max(10, rng.gauss(COMP_MEAN, COMP_STD))
        time.sleep(comp_time / 1000.0)

        load_start = time.time()

        prefetch_hit = False
        with prefetch_lock:
            if batch_id in prefetch_arrived:
                prefetch_hit = True

        if prefetch_hit:
            load_time_ms = max(1, rng.gauss(3, 1))
            time.sleep(load_time_ms / 1000.0)
        else:
            obj_id = (batch_id * 256 + TASK_ID) % 256
            pkt = make_tdtp_read(TASK_ID, batch_id, obj_id)
            sendp(pkt, iface=IFACE, verbose=0)

            wait_start = time.time()
            timeout_sec = (STOR_LATENCY * 5 + 500) / 1000.0
            while (time.time() - wait_start) < timeout_sec:
                with prefetch_lock:
                    if batch_id in prefetch_arrived:
                        break
                time.sleep(0.002)

        actual_load_ms = (time.time() - load_start) * 1000

        if MODE == 'inisa':
            status_pkt = make_tdtp_status(
                TASK_ID, batch_id,
                int(comp_time * 1000),
                int(actual_load_ms * 1000))
            sendp(status_pkt, iface=IFACE, verbose=0)

        records.append({
            'batch_id': batch_id,
            'comp_ms': int(comp_time),
            'load_ms': int(actual_load_ms),
            'hit': 1 if prefetch_hit else 0,
            'total': TOTAL_BATCHES,
        })

        if batch_id % 10 == 0 or batch_id < 5:
            hit_str = "HIT" if prefetch_hit else "MISS"
            print(f'[GPU-{TASK_ID}] Batch {batch_id}/{TOTAL_BATCHES} '
                  f'comp={int(comp_time)}ms load={int(actual_load_ms)}ms '
                  f'[{hit_str}]', flush=True)

    with open(OUTPUT_CSV, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['batch_id','total','comp_ms','load_ms','hit'])
        w.writeheader()
        w.writerows(records)

    hits = sum(1 for r in records if r['hit'])
    avg_load = sum(r['load_ms'] for r in records) / len(records)
    avg_comp = sum(r['comp_ms'] for r in records) / len(records)
    print(f'[GPU-{TASK_ID}] COMPLETED: {hits}/{TOTAL_BATCHES} hits '
          f'({hits/TOTAL_BATCHES*100:.1f}%), '
          f'avg_load={avg_load:.0f}ms, avg_comp={avg_comp:.0f}ms', flush=True)

    stop_sniffer.set()

if __name__ == '__main__':
    simulate_training()