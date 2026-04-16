import sys
sys.path.append(".")
#!/usr/bin/env python3
# status_listener.py - 控制平面：监听 STATUS + 独立测量 PREFETCH 往返时间
import sys, time, threading
sys.path.append('..')
from scapy.all import sniff, sendp, UDP, bind_layers
from hosts.tdtp_packet import *
from controller.prefetch_engine import PrefetchEngine

bind_layers(UDP, TDTP, dport=9999)

IFACE = sys.argv[1] if len(sys.argv) > 1 else 'eth0'
FIXED_DEPTH = int(sys.argv[2]) if len(sys.argv) > 2 else 0  # 0=EWMA自适应, >0=固定深度

from scapy.all import conf

l2_sock = conf.L2socket(iface=IFACE)
_last_b = {}

# ★ 控制平面独立测量 PREFETCH 往返时间
prefetch_send_time = {}    # (task_id, batch_id) -> send_timestamp
measured_storage_lat = {}  # task_id -> 最近测量的存储延迟(ms)
measure_lock = threading.Lock()

def send_prefetch_pkt(task_id, batch_id, obj_id):
    global _last_b
    pkt = make_tdtp_prefetch(task_id, batch_id, obj_id)

    # 记录发送时间戳
    with measure_lock:
        prefetch_send_time[(task_id, batch_id)] = time.time()

    l2_sock.send(pkt)

    if _last_b.get(task_id) != batch_id:
        print(f"[-->] PREFETCH: Task={task_id} Batch={batch_id} Obj={obj_id}")
        _last_b[task_id] = batch_id

def ack_monitor():
    """
    后台线程：监听 PREFETCH 的 ACK 返回，计算往返时间。
    控制平面发出 PREFETCH 时记录时间戳，收到 ACK 时计算差值。
    """
    def handle_ack(pkt):
        if not pkt.haslayer(UDP):
            return
        try:
            payload = bytes(pkt[UDP].payload)
            if not payload:
                return
            tdtp = TDTP(payload)
            if tdtp.opcode != TDTP_ACK:
                return

            key = (tdtp.taskId, tdtp.batchId)
            with measure_lock:
                if key in prefetch_send_time:
                    rtt_ms = (time.time() - prefetch_send_time[key]) * 1000
                    measured_storage_lat[tdtp.taskId] = rtt_ms
                    del prefetch_send_time[key]
        except:
            pass

    sniff(iface=IFACE, filter='udp', prn=handle_ack)

engine = PrefetchEngine(send_prefetch_pkt, fixed_depth=FIXED_DEPTH)

for tid in range(1, 9):
    engine.register_task(task_id=tid, total_samples=500*256, batch_size=256)

def handle_pkt(pkt):
    if pkt.haslayer(UDP) and pkt[UDP].dport == 9999:
        try:
            payload_bytes = bytes(pkt[UDP].payload)
            tdtp_layer = TDTP(payload_bytes)

            if tdtp_layer.opcode == 0x03:
                task_id = tdtp_layer.taskId
                batch_id = tdtp_layer.batchId
                comp_time_ms = tdtp_layer.offset / 1000.0

                # ★ 优先使用控制平面独立测量的存储延迟
                with measure_lock:
                    if task_id in measured_storage_lat:
                        load_time_ms = measured_storage_lat[task_id]
                    else:
                        load_time_ms = tdtp_layer.length / 1000.0

                engine.on_status_report(task_id, batch_id, comp_time_ms, load_time_ms)
                sys.stdout.flush()
        except Exception as e:
            print(f"[ERROR] {e}")
            sys.stdout.flush()

if __name__ == '__main__':
    mode_str = f"Fixed D={FIXED_DEPTH}" if FIXED_DEPTH > 0 else "EWMA Adaptive"
    print(f"[*] INISA Controller started, iface: {IFACE}, mode: {mode_str}")
    print(f"[*] RTT measurement enabled (independent storage latency tracking)")
    sys.stdout.flush()

    ack_thread = threading.Thread(target=ack_monitor, daemon=True)
    ack_thread.start()

    sniff(iface=IFACE, filter="udp port 9999", prn=handle_pkt)