#!/usr/bin/env python3
"""
storage_server_real.py - 存储节点（真实数据版）
==============================================
预加载 CIFAR-10 图片到内存，收到 READ/PREFETCH 时返回真实图片数据。
ACK 报文包含完整的 3073 字节样本数据（1B标签 + 3072B像素）。

用法:
  python3 hosts/storage_server_real.py <iface> <base_lat_ms> <jitter_ms> <store_id>
"""
import sys, os, time, random, threading, struct, glob
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scapy.all import *
from scapy.all import conf
from hosts.tdtp_packet import *

IFACE       = sys.argv[1]        if len(sys.argv) > 1 else 'eth0'
BASE_LAT_MS = float(sys.argv[2]) if len(sys.argv) > 2 else 50.0
JITTER_MS   = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
STORE_ID    = int(sys.argv[4])   if len(sys.argv) > 4 else 1
CONG_FILE   = sys.argv[5]        if len(sys.argv) > 5 else ''

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', f'store{STORE_ID}')

l2_sock = conf.L2socket(iface=IFACE)
req_count = 0

# ★ 预加载所有样本数据到内存
sample_cache = {}

def preload_data():
    """启动时将本节点的所有样本加载到内存"""
    global sample_cache
    if not os.path.exists(DATA_DIR):
        print(f"[Store-{STORE_ID}] WARNING: Data dir {DATA_DIR} not found, using dummy data", flush=True)
        return
    
    files = glob.glob(os.path.join(DATA_DIR, 'sample_*.bin'))
    for fpath in files:
        fname = os.path.basename(fpath)
        # 从文件名提取 sample_id: sample_12345.bin -> 12345
        sample_id = int(fname.split('_')[1].split('.')[0])
        with open(fpath, 'rb') as f:
            sample_cache[sample_id] = f.read()
    
    print(f"[Store-{STORE_ID}] Preloaded {len(sample_cache)} samples "
          f"({len(sample_cache) * 3073 / 1024 / 1024:.1f} MB)", flush=True)

def get_sample_data(object_id):
    """根据 objectId 获取样本数据"""
    # objectId 映射到 sample_id: 在真实系统中由元数据服务维护
    # 这里简化为: sample_id = objectId (objectId 就是全局样本索引)
    if object_id in sample_cache:
        return sample_cache[object_id]
    
    # 如果没有预加载数据，返回 3073 字节的伪数据
    return struct.pack('B', object_id % 10) + bytes([object_id % 256] * 3072)

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

        # ★ 获取真实样本数据
        sample_data = get_sample_data(tdtp.objectId)

        # ACK 报文 = TDTP 头部 + 真实样本数据作为 payload
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
                     length=len(sample_data),
                     flags=0) /
                Raw(sample_data))  # ★ 附加真实图片数据

        threading.Thread(target=delayed_send, args=(resp, lat), daemon=True).start()

        if req_count % 20 == 0:
            op = "READ" if tdtp.opcode == TDTP_READ else "PREFETCH"
            print(f"[Store-{STORE_ID}] #{req_count} {op} task={tdtp.taskId} "
                  f"batch={tdtp.batchId} obj={tdtp.objectId} "
                  f"data={len(sample_data)}B lat={lat:.0f}ms", flush=True)

    except Exception as e:
        print(f"[Store-{STORE_ID}] ERROR: {e}", flush=True)

if __name__ == '__main__':
    preload_data()
    print(f"[Store-{STORE_ID}] Started: iface={IFACE}, lat={BASE_LAT_MS}±{JITTER_MS}ms, "
          f"data_dir={DATA_DIR}", flush=True)
    sniff(iface=IFACE, filter="udp", prn=handle)
