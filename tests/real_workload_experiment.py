#!/usr/bin/env python3
"""
INISA Real Workload Experiment Driver
======================================
Layer A: 存储节点传输真实 CIFAR-10 图片数据（功能验证）
Layer B: GPU 节点执行真实 PyTorch 训练，TDTP 作为预取信令（性能验证）

运行:
  cd inisa-prototype-v4
  python3 hosts/data_prep.py                        # 步骤1: 准备数据
  sudo python3 tests/real_workload_experiment.py     # 步骤2: 运行实验

产出: results_real/ 目录下 CSV 数据 + PDF 图表
"""
import os, sys, time, csv, subprocess, re, threading
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, '..')
sys.path.insert(0, PROJECT_ROOT)

P4_JSON  = os.path.join(PROJECT_ROOT, 'p4src', 'inisa.json')
P4_SRC   = os.path.join(PROJECT_ROOT, 'p4src', 'inisa.p4')
RESULTS  = os.path.join(PROJECT_ROOT, 'results_real')
LOGS     = os.path.join(RESULTS, 'logs')
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')
os.makedirs(RESULTS, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)

# ============================================================
# 1. 检查前置条件
# ============================================================
def check_prerequisites():
    print("[0] Checking prerequisites...")
    
    if not os.path.exists(os.path.join(DATA_DIR, 'store1')):
        print("    ERROR: Data not prepared! Run: python3 hosts/data_prep.py")
        return False
    
    n_samples = len([f for f in os.listdir(os.path.join(DATA_DIR, 'store1'))
                     if f.endswith('.bin')])
    print(f"    CIFAR-10 data ready: {n_samples} samples per store node")
    
    try:
        import torch
        print(f"    PyTorch {torch.__version__} available (CPU mode)")
    except ImportError:
        print("    WARNING: PyTorch not installed. Layer B will use computation simulation.")
    
    if not os.path.exists(P4_JSON):
        print("    Compiling P4...")
        r = subprocess.run(f"p4c-bm2-ss --p4v 16 --std p4-16 -o {P4_JSON} {P4_SRC}",
                           shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"    ERROR: P4 compilation failed: {r.stderr}")
            return False
    print("    P4 JSON ready")
    
    return True

# ============================================================
# 2. Mininet 网络
# ============================================================
def setup_network():
    print("[1] Setting up Mininet + BMv2...")
    os.system("mn -c 2>/dev/null"); os.system("pkill -9 -f simple_switch 2>/dev/null")
    time.sleep(1)
    from mininet.net import Mininet
    from mininet.node import Switch
    from mininet.link import TCLink
    from mininet.log import setLogLevel
    setLogLevel('warning')

    class BMv2(Switch):
        def __init__(self, name, **kw):
            Switch.__init__(self, name, **kw)
            self.thrift_port = 9090
        def start(self, controllers):
            ifaces = ' '.join(f'-i {p}@{i}' for p,i in self.intfs.items() if p>0)
            log = os.path.join(LOGS, 'bmv2.log')
            self.cmd(f'simple_switch {ifaces} {P4_JSON} '
                     f'--thrift-port {self.thrift_port} --log-console > {log} 2>&1 &')
            time.sleep(3); print(f"    BMv2 started (thrift={self.thrift_port})")
        def stop(self):
            self.cmd('pkill -f simple_switch'); Switch.stop(self)

    net = Mininet(controller=None, link=TCLink)
    s1 = net.addSwitch('s1', cls=BMv2)
    hosts = {}
    for i in range(1, 5):
        h = net.addHost(f'h{i}', ip=f'10.0.1.{i}/24', mac=f'00:00:00:00:01:{i:02x}')
        net.addLink(h, s1, port2=i); hosts[f'h{i}'] = h
    for i in range(1, 5):
        h = net.addHost(f'st{i}', ip=f'10.0.2.{i}/24', mac=f'00:00:00:00:02:{i:02x}')
        net.addLink(h, s1, port2=i+4); hosts[f'st{i}'] = h

    net.start()
    
    # 解除 MTU 限制，允许真实图片数据通过
    for i in range(1, 9):
        s1.cmd(f'ifconfig s1-eth{i} mtu 9000')
    for h in hosts.values():
        h.cmd(f'ifconfig {h.defaultIntf()} mtu 9000')
        for j in range(1, 5):
            h.cmd(f'arp -s 10.0.1.{j} 00:00:00:00:01:{j:02x}')
            h.cmd(f'arp -s 10.0.2.{j} 00:00:00:00:02:{j:02x}')

    print(f"    Network: h1-h4 (GPU) <-> s1 (P4) <-> st1-st4 (Storage)")
    return net, s1, hosts

# ============================================================
# 3. 表项安装
# ============================================================
def install_tables(sw, num_obj=256):
    print("[2] Installing table entries...")
    entries = []
    for oid in range(num_obj):
        sid = (oid % 4) + 1
        ip_hex = f'0x0a0002{sid:02x}'
        port = sid + 4
        entries.append(f"table_add InisaIngress.object_address_translation "
                       f"InisaIngress.set_storage_dest {oid} => {ip_hex} 9999 {port}")
    for tid in range(1, 9):
        gpu_id = ((tid - 1) % 4) + 1
        gpu_ip_hex = f'0x0a0001{gpu_id:02x}'
        gpu_port = gpu_id
        entries.append(f"table_add InisaIngress.task_to_gpu "
                       f"InisaIngress.set_gpu_dest {tid} => {gpu_ip_hex} 9999 {gpu_port}")
    for tid in range(1, 9):
        entries.append(f"table_add InisaIngress.task_metering "
                       f"InisaIngress.read_task_meter {tid} =>")
    entries.append("mirroring_add 100 5")
    cli = '\n'.join(entries) + '\n'
    subprocess.run(f'simple_switch_CLI --thrift-port {sw.thrift_port}',
                   input=cli, shell=True, capture_output=True, text=True)
    print(f"    Installed {len(entries)} entries")

# ============================================================
# 4. 清理辅助
# ============================================================
def cleanup_processes(hosts):
    for h in hosts.values():
        h.cmd('pkill -9 -f gpu_trainer_real; pkill -9 -f gpu_client_v3; '
              'pkill -9 -f storage_server_real; pkill -9 -f storage_server_v3; '
              'pkill -9 -f status_listener')
    time.sleep(2)

# ============================================================
# 5. Layer A: 真实数据传输验证
# ============================================================
def run_layer_a(hosts):
    """Layer A: 存储节点传输真实 CIFAR-10 图片，验证 TDTP 能承载真实训练数据"""
    print("\n" + "=" * 60)
    print("  Layer A: Real Data Transport Verification")
    print("=" * 60)
    
    cleanup_processes(hosts)
    
    # ★ 启动真实数据存储节点（storage_server_real.py，返回3073字节图片）
    for i in range(1, 5):
        st = hosts[f'st{i}']
        log = os.path.join(LOGS, f'layer_a_st{i}.log')
        st.cmd(f'cd {PROJECT_ROOT} && python3 hosts/storage_server_real.py '
               f'{st.defaultIntf()} 50 5 {i} > {log} 2>&1 &')
    time.sleep(2)
    
    # 启动控制器
    ctrl_host = hosts['st1']
    ctrl_log = os.path.join(LOGS, 'layer_a_ctrl.log')
    ctrl_host.cmd(f'cd {PROJECT_ROOT} && python3 -u controller/status_listener.py '
                  f'{ctrl_host.defaultIntf()} 0 > {ctrl_log} 2>&1 &')
    time.sleep(1)
    
    # 启动 GPU 客户端 (30 -> 300)
    h1 = hosts['h1']
    csv_path = os.path.join(RESULTS, 'layer_a_transport_h1.csv')
    h1_log = os.path.join(LOGS, 'layer_a_h1.log')
    h1.cmd(f'cd {PROJECT_ROOT} && python3 hosts/gpu_client_v3.py '
           f'1 {h1.defaultIntf()} 300 200 15 50 inisa {csv_path} '
           f'> {h1_log} 2>&1 &')
    
    print("    Running Layer A (300 batches with real data transport)...")
    timeout = 600  # 超时拉长到 10 分钟
    elapsed = 0
    while elapsed < timeout:
        time.sleep(5); elapsed += 5
        if os.path.exists(h1_log):
            with open(h1_log) as f:
                if 'COMPLETED' in f.read():
                    print(f"    Layer A finished in {elapsed}s")
                    break
        if elapsed % 20 < 5:
            print(f"    Waiting... {elapsed}s")
    
    cleanup_processes(hosts)
    
    if os.path.exists(csv_path):
        with open(csv_path) as f:
            records = list(csv.DictReader(f))
        hits = sum(1 for r in records if int(r.get('hit', 0)))
        avg_load = sum(int(r['load_ms']) for r in records) / len(records)
        print(f"    Result: {len(records)} batches, {hits} hits, avg_load={avg_load:.0f}ms")
        print(f"    Real CIFAR-10 data successfully transported through BMv2 P4 switch")
    else:
        print(f"    No results generated")

# ============================================================
# 6. Layer B: 真实 PyTorch 训练
# ============================================================
def run_layer_b(hosts):
    """Layer B: 真实 PyTorch CNN 训练，TDTP 作为预取信令协议"""
    print("\n" + "=" * 60)
    print("  Layer B: Real PyTorch Training via TDTP/BMv2")
    print("  (TDTP as control signaling, PyTorch uses local data)")
    print("=" * 60)
    
    # --- Baseline: 无预取 ---
    cleanup_processes(hosts)
    
    # ★ Layer B 用轻量级存储服务器（TDTP 作为信令协议）
    for i in range(1, 5):
        st = hosts[f'st{i}']
        log = os.path.join(LOGS, f'layer_b_base_st{i}.log')
        st.cmd(f'cd {PROJECT_ROOT} && python3 hosts/storage_server_v3.py '
               f'{st.defaultIntf()} 150 10 /dev/null > {log} 2>&1 &')
    time.sleep(2)
    
    h1 = hosts['h1']
    csv_base = os.path.join(RESULTS, 'layer_b_baseline_h1.csv')
    h1_log = os.path.join(LOGS, 'layer_b_baseline_h1.log')
    # 30 -> 300
    h1.cmd(f'cd {PROJECT_ROOT} && python3 hosts/gpu_trainer_real.py '
           f'1 {h1.defaultIntf()} 300 16 150 baseline {csv_base} '
           f'> {h1_log} 2>&1 &')
    
    print("    Running Baseline training (300 batches, no prefetch)...")
    timeout = 900  # 超时拉长到 15 分钟
    elapsed = 0
    while elapsed < timeout:
        time.sleep(5); elapsed += 5
        if os.path.exists(h1_log):
            with open(h1_log) as f:
                if 'COMPLETED' in f.read():
                    print(f"    Baseline finished in {elapsed}s"); break
        if elapsed % 20 < 5:
            print(f"    Waiting... {elapsed}s")
    
    cleanup_processes(hosts)
    
    # --- INISA: 预取启用 ---
    for i in range(1, 5):
        st = hosts[f'st{i}']
        log = os.path.join(LOGS, f'layer_b_inisa_st{i}.log')
        st.cmd(f'cd {PROJECT_ROOT} && python3 hosts/storage_server_v3.py '
               f'{st.defaultIntf()} 150 10 /dev/null > {log} 2>&1 &')
    time.sleep(2)
    
    ctrl_host = hosts['st1']
    ctrl_log = os.path.join(LOGS, 'layer_b_inisa_ctrl.log')
    ctrl_host.cmd(f'cd {PROJECT_ROOT} && python3 -u controller/status_listener.py '
                  f'{ctrl_host.defaultIntf()} 0 > {ctrl_log} 2>&1 &')
    time.sleep(1)
    
    csv_inisa = os.path.join(RESULTS, 'layer_b_inisa_h1.csv')
    h1_log = os.path.join(LOGS, 'layer_b_inisa_h1.log')
    # 30 -> 300
    h1.cmd(f'cd {PROJECT_ROOT} && python3 hosts/gpu_trainer_real.py '
           f'1 {h1.defaultIntf()} 300 16 150 inisa {csv_inisa} '
           f'> {h1_log} 2>&1 &')
    
    print("    Running INISA training (300 batches, prefetch enabled)...")
    elapsed = 0
    while elapsed < timeout:
        time.sleep(5); elapsed += 5
        if os.path.exists(h1_log):
            with open(h1_log) as f:
                if 'COMPLETED' in f.read():
                    print(f"    INISA finished in {elapsed}s"); break
        if elapsed % 20 < 5:
            print(f"    Waiting... {elapsed}s")
    
    cleanup_processes(hosts)

    # --- Fixed D=2: 固定深度预取（消融对比） ---
    cleanup_processes(hosts)
    
    for i in range(1, 5):
        st = hosts[f'st{i}']
        log = os.path.join(LOGS, f'layer_b_fixed_d2_st{i}.log')
        st.cmd(f'cd {PROJECT_ROOT} && python3 hosts/storage_server_v3.py '
               f'{st.defaultIntf()} 150 10 /dev/null > {log} 2>&1 &')
    time.sleep(2)
    
    ctrl_host = hosts['st1']
    ctrl_log = os.path.join(LOGS, 'layer_b_fixed_d2_ctrl.log')
    ctrl_host.cmd(f'cd {PROJECT_ROOT} && python3 -u controller/status_listener.py '
                  f'{ctrl_host.defaultIntf()} 2 > {ctrl_log} 2>&1 &')
    time.sleep(1)
    
    csv_fixed = os.path.join(RESULTS, 'layer_b_fixed_d2_h1.csv')
    h1_log = os.path.join(LOGS, 'layer_b_fixed_d2_h1.log')
    h1.cmd(f'cd {PROJECT_ROOT} && python3 hosts/gpu_trainer_real.py '
           f'1 {h1.defaultIntf()} 300 16 150 inisa {csv_fixed} '
           f'> {h1_log} 2>&1 &')
    
    print("    Running Fixed D=2 training (real PyTorch)...")
    elapsed = 0
    while elapsed < timeout:
        time.sleep(5); elapsed += 5
        if os.path.exists(h1_log):
            with open(h1_log) as f:
                if 'COMPLETED' in f.read():
                    print(f"    Fixed D=2 finished in {elapsed}s"); break
        if elapsed % 20 < 5:
            print(f"    Waiting... {elapsed}s")
    
    cleanup_processes(hosts)

# ============================================================
# 7. 画图
# ============================================================
def plot_results():
    print("\n[Plot] Generating real workload figures...")
    try:
        import matplotlib; matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams.update({
            'font.size':11, 'font.family':'serif',
            'figure.dpi':300, 'savefig.dpi':300, 'savefig.bbox':'tight',
            'axes.grid':True, 'grid.alpha':0.25, 'lines.linewidth':2,
        })
    except ImportError:
        print("    matplotlib not installed, skipping."); return

    def read_csv_file(path):
        if not os.path.exists(path): return []
        with open(path) as f: return list(csv.DictReader(f))

    def mavg(vals, w=5):
        r=[]
        for i in range(len(vals)):
            s=max(0,i-w+1); r.append(sum(vals[s:i+1])/(i-s+1))
        return r

    # === Layer A: Data Transport Verification ===
    layer_a = read_csv_file(os.path.join(RESULTS, 'layer_a_transport_h1.csv'))
    if layer_a:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))
        
        bids = [int(x['batch_id']) for x in layer_a]
        loads = [int(x['load_ms']) for x in layer_a]
        hits = [int(x.get('hit', 0)) for x in layer_a]
        
        colors_scatter = ['#1f77b4' if h else '#d62728' for h in hits]
        ax1.scatter(bids, loads, c=colors_scatter, s=40, zorder=5, alpha=0.8)
        ax1.plot(bids, mavg(loads, 5), color='gray', ls='-', lw=1.5, alpha=0.5)
        ax1.set_xlabel('Iteration')
        ax1.set_ylabel('Data Load Latency (ms)')
        ax1.set_title('(a) Real CIFAR-10 Data Transport Latency')
        from matplotlib.lines import Line2D
        legend_elements = [Line2D([0], [0], marker='o', color='w', markerfacecolor='#1f77b4',
                                   markersize=8, label='Prefetch HIT'),
                           Line2D([0], [0], marker='o', color='w', markerfacecolor='#d62728',
                                   markersize=8, label='Prefetch MISS')]
        ax1.legend(handles=legend_elements)
        
        cum_hits = []
        h = 0
        for i, hit in enumerate(hits):
            h += hit
            cum_hits.append(h / (i + 1) * 100)
        ax2.plot(bids, cum_hits, color='#2ca02c', lw=2.5)
        ax2.set_xlabel('Iteration')
        ax2.set_ylabel('Cumulative Hit Rate (%)')
        ax2.set_title('(b) Prefetch Hit Rate (Real Data Payload)')
        ax2.set_ylim(0, 105)
        ax2.axhline(y=100, color='gray', ls=':', alpha=0.3)
        
        total_batches = len(layer_a)
        ax2.text(0.95, 0.15, f'Payload: 3073 B/sample\n(1B label + 3072B pixels)\n'
                 f'Total: {total_batches} batches',
                 transform=ax2.transAxes, ha='right', va='bottom',
                 fontsize=9, bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        
        plt.suptitle('Layer A: Real CIFAR-10 Data Transport through BMv2 P4 Switch', fontsize=13)
        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig_real_transport.pdf'))
        plt.close()
        print("    [FIG] fig_real_transport.pdf")

    # === Layer B: Real PyTorch Training ===
    base = read_csv_file(os.path.join(RESULTS, 'layer_b_baseline_h1.csv'))
    inisa = read_csv_file(os.path.join(RESULTS, 'layer_b_inisa_h1.csv'))
    
    if base and inisa:
        fig, axes = plt.subplots(2, 2, figsize=(13, 9))
        
        for data, label, color, ls in [(base, 'Baseline', '#d62728', '--'),
                                        (inisa, 'INISA', '#1f77b4', '-')]:
            bids = [int(x['batch_id']) for x in data]
            loads = [int(x['load_ms']) for x in data]
            comps = [int(x['comp_ms']) for x in data]
            iters = [c + l for c, l in zip(comps, loads)]
            
            axes[0][0].plot(bids, mavg(loads, 5), color=color, ls=ls, label=label, lw=2)
            axes[0][1].plot(bids, mavg(comps, 5), color=color, ls=ls, label=label, lw=2)
            
            tp = [16 / (t / 1000) if t > 0 else 0 for t in iters]
            axes[1][0].plot(bids, mavg(tp, 5), color=color, ls=ls, label=label, lw=2)
            
            if 'loss' in data[0]:
                losses = [float(x['loss']) for x in data]
                axes[1][1].plot(bids, losses, color=color, ls=ls, label=label, lw=2)

        axes[0][0].set_ylabel('Load Latency (ms)'); axes[0][0].set_title('(a) Data Load Latency')
        axes[0][0].legend()
        axes[0][1].set_ylabel('Compute Time (ms)'); axes[0][1].set_title('(b) Computation Time (Real PyTorch)')
        axes[0][1].legend()
        axes[1][0].set_xlabel('Iteration'); axes[1][0].set_ylabel('Throughput (samples/sec)')
        axes[1][0].set_title('(c) Training Throughput'); axes[1][0].legend()
        axes[1][1].set_xlabel('Iteration'); axes[1][1].set_ylabel('Training Loss')
        axes[1][1].set_title('(d) Training Loss Convergence'); axes[1][1].legend()
        
        plt.suptitle('Real PyTorch CIFAR-10 Training via TDTP/BMv2 P4 Switch', fontsize=14)
        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig_real_training.pdf'))
        plt.close()
        print("    [FIG] fig_real_training.pdf")


    # === Real PyTorch Ablation ===
    base = read_csv_file(os.path.join(RESULTS, 'layer_b_baseline_h1.csv'))
    fixed = read_csv_file(os.path.join(RESULTS, 'layer_b_fixed_d2_h1.csv'))
    ewma = read_csv_file(os.path.join(RESULTS, 'layer_b_inisa_h1.csv'))
    
    if base and fixed and ewma:
        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(15, 4.5))
        
        configs = [
            ('No Prefetch\n(Baseline)', base, '#d62728'),
            ('Fixed D=2', fixed, '#ff7f0e'),
            ('EWMA Adaptive\n(INISA)', ewma, '#1f77b4'),
        ]
        
        names = []; lats = []; tps = []; hrs = []; colors = []
        skip = 10
        for label, data, color in configs:
            r = data[skip:]
            if not r: continue
            names.append(label)
            colors.append(color)
            lats.append(sum(int(x['load_ms']) for x in r) / len(r))
            hits = sum(1 for x in r if int(x.get('hit', 0)))
            hrs.append(hits / len(r) * 100)
            iters = [int(x['comp_ms']) + int(x['load_ms']) for x in r]
            tps.append(sum(16 / (t / 1000) if t > 0 else 0 for t in iters) / len(iters))
        
        # (a) Load Latency
        bars1 = ax1.bar(range(len(names)), lats, color=colors)
        for b, v in zip(bars1, lats):
            ax1.text(b.get_x() + b.get_width()/2, b.get_height() + 3,
                     f'{v:.0f}ms', ha='center', fontsize=11, fontweight='bold')
        ax1.set_xticks(range(len(names))); ax1.set_xticklabels(names, fontsize=9)
        ax1.set_ylabel('Avg Load Latency (ms)')
        ax1.set_title('(a) Load Latency (Real PyTorch)')
        
        # (b) Throughput
        bars2 = ax2.bar(range(len(names)), tps, color=colors)
        for b, v in zip(bars2, tps):
            ax2.text(b.get_x() + b.get_width()/2, b.get_height() + 0.5,
                     f'{v:.1f}', ha='center', fontsize=11, fontweight='bold')
        ax2.set_xticks(range(len(names))); ax2.set_xticklabels(names, fontsize=9)
        ax2.set_ylabel('Throughput (samples/sec)')
        ax2.set_title('(b) Throughput (Real PyTorch)')
        
        # (c) Hit Rate
        bars3 = ax3.bar(range(len(names)), hrs, color=colors)
        for b, v in zip(bars3, hrs):
            ax3.text(b.get_x() + b.get_width()/2, b.get_height() + 1,
                     f'{v:.1f}%', ha='center', fontsize=11, fontweight='bold')
        ax3.set_xticks(range(len(names))); ax3.set_xticklabels(names, fontsize=9)
        ax3.set_ylabel('Prefetch Hit Rate (%)')
        ax3.set_title('(c) Hit Rate (Real PyTorch)')
        ax3.set_ylim(0, 110)
        
        plt.suptitle('Ablation Study with Real PyTorch CIFAR-10 Training', fontsize=14)
        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig_real_ablation.pdf'))
        plt.close()
        print("    [FIG] fig_real_ablation.pdf")

    # 汇总
    print("\n  === Real Workload Summary ===")
    for fn in ['layer_a_transport_h1.csv', 'layer_b_baseline_h1.csv',
               'layer_b_fixed_d2_h1.csv', 'layer_b_inisa_h1.csv']:
        data = read_csv_file(os.path.join(RESULTS, fn))
        if data:
            hits = sum(1 for x in data if int(x.get('hit', 0)))
            avg_load = sum(int(x['load_ms']) for x in data) / len(data)
            avg_comp = sum(int(x['comp_ms']) for x in data) / len(data)
            print(f"  {fn:<35} batches={len(data)}, hits={hits}, "
                  f"avg_load={avg_load:.0f}ms, avg_comp={avg_comp:.0f}ms")

# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("  INISA Real Workload Experiment")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if os.geteuid() != 0:
        print("ERROR: sudo required."); sys.exit(1)

    if not check_prerequisites():
        sys.exit(1)

    net, sw, hosts = setup_network()

    try:
        install_tables(sw)
        t0 = time.time()
        
        run_layer_a(hosts)
        run_layer_b(hosts)
        
        print(f"\n  Total time: {(time.time()-t0)/60:.1f} minutes")
        plot_results()
        
        print(f"\n  Results in {RESULTS}/:")
        for f in sorted(os.listdir(RESULTS)):
            if not os.path.isdir(os.path.join(RESULTS, f)):
                sz = os.path.getsize(os.path.join(RESULTS, f))
                print(f"    {f:<40} {sz/1024:.1f} KB")
    
    except KeyboardInterrupt:
        print("\n  Interrupted.")
    except Exception as e:
        print(f"\n  ERROR: {e}")
        import traceback; traceback.print_exc()
    finally:
        print("\n  Cleaning up...")
        cleanup_processes(hosts)
        net.stop(); os.system("mn -c 2>/dev/null")

if __name__ == '__main__':
    main()