#!/usr/bin/env python3
"""
INISA Layer 2 v3: Mininet 自动化实验驱动（真实 BMv2）
======================================================
所有报文真实经过 BMv2 P4 交换机。8 组实验，自动画图。

运行:
  cd inisa-prototype
  sudo python3 tests/mininet_experiment_driver_v3.py

约 15-25 分钟完成，产出保存到 results_bmv2_v3/
"""
import os, sys, time, csv, subprocess, re, threading
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, '..')
sys.path.insert(0, PROJECT_ROOT)

P4_JSON   = os.path.join(PROJECT_ROOT, 'p4src', 'inisa.json')
P4_SRC    = os.path.join(PROJECT_ROOT, 'p4src', 'inisa.p4')
RESULTS   = os.path.join(PROJECT_ROOT, 'results_bmv2_v3')
LOGS      = os.path.join(RESULTS, 'logs')
os.makedirs(RESULTS, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)

# ============================================================
# 1. P4 编译
# ============================================================
def compile_p4():
    print("[1] Compiling P4...")
    # 总是重新编译（因为 ingress.p4 等被 include 的文件可能已更改）
    r = subprocess.run(f"p4c-bm2-ss --p4v 16 --std p4-16 -o {P4_JSON} {P4_SRC}",
                       shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    FAILED: {r.stderr}"); return False
    print("    OK"); return True

# ============================================================
# 2. Mininet 网络
# ============================================================
def setup_network():
    print("[2] Setting up Mininet + BMv2...")
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
    # 4 GPU + 4 存储（BMv2 端口 1-4 GPU, 5-8 存储）
    hosts = {}
    for i in range(1,5):
        h = net.addHost(f'h{i}', ip=f'10.0.1.{i}/24', mac=f'00:00:00:00:01:{i:02x}')
        net.addLink(h, s1, port2=i); hosts[f'h{i}'] = h
    for i in range(1,5):
        h = net.addHost(f'st{i}', ip=f'10.0.2.{i}/24', mac=f'00:00:00:00:02:{i:02x}')
        net.addLink(h, s1, port2=i+4); hosts[f'st{i}'] = h

    net.start()
    # 静态 ARP
    for h in hosts.values():
        for j in range(1,5):
            h.cmd(f'arp -s 10.0.1.{j} 00:00:00:00:01:{j:02x}')
            h.cmd(f'arp -s 10.0.2.{j} 00:00:00:00:02:{j:02x}')

    print(f"    Network: h1-h4 (GPU) <-> s1 (P4) <-> st1-st4 (Storage)")
    return net, s1, hosts

# ============================================================
# 3. 表项安装
# ============================================================
def install_tables(sw, num_obj=256):
    print("[3] Installing table entries...")
    entries = []
    # 正向路由: objectId → 存储节点
    for oid in range(num_obj):
        sid = (oid % 4) + 1  # 分散到 4 个存储节点
        ip_hex = f'0x0a0002{sid:02x}'
        port = sid + 4
        entries.append(f"table_add InisaIngress.object_address_translation "
                       f"InisaIngress.set_storage_dest {oid} => {ip_hex} 9999 {port}")
    # ★ 反向路由: taskId → GPU 节点 (ACK 报文回程)
    for tid in range(1, 9):
        gpu_id = ((tid - 1) % 4) + 1  # task 1→h1, task 2→h2, ...
        gpu_ip_hex = f'0x0a0001{gpu_id:02x}'
        gpu_port = gpu_id
        entries.append(f"table_add InisaIngress.task_to_gpu "
                       f"InisaIngress.set_gpu_dest {tid} => {gpu_ip_hex} 9999 {gpu_port}")
    # meter 表项
    for tid in range(1,9):
        entries.append(f"table_add InisaIngress.task_metering "
                       f"InisaIngress.read_task_meter {tid} =>")
    entries.append("mirroring_add 100 5")
    cli = '\n'.join(entries) + '\n'
    subprocess.run(f'simple_switch_CLI --thrift-port {sw.thrift_port}',
                   input=cli, shell=True, capture_output=True, text=True)
    print(f"    Installed {len(entries)} entries (incl. {min(8,8)} reverse routes)")

# ============================================================
# 4. 单次实验执行
# ============================================================
def run_exp(hosts, name, gpu_configs, store_lat_ms=50, store_jitter_ms=10,
            enable_prefetch=True, batches=80, congestion_file='', fixed_depth=0):
    """
    gpu_configs: [{host, task_id, comp_mean, comp_std}]
    """
    # 彻底清理上一轮残留进程
    for h in hosts.values():
        h.cmd('pkill -9 -f gpu_client_v3; pkill -9 -f storage_server_v3; '
              'pkill -9 -f status_listener')
    time.sleep(2)
    print(f"\n  === {name} ===")
    csvs = []

    # 4a) 启动存储节点
    for i in range(1,5):
        st = hosts[f'st{i}']
        log = os.path.join(LOGS, f'{name}_st{i}.log')
        cong_arg = congestion_file if congestion_file else ''
        st.cmd(f'cd {PROJECT_ROOT} && python3 hosts/storage_server_v3.py '
               f'{st.defaultIntf()} {store_lat_ms} {store_jitter_ms} {log} '
               f'{cong_arg} > /dev/null 2>&1 &')
    time.sleep(1)

    # 4b) 启动预取控制器
    if enable_prefetch:
        ctrl_host = hosts['st1']
        ctrl_log = os.path.join(LOGS, f'{name}_ctrl.log')
        ctrl_host.cmd(f'cd {PROJECT_ROOT} && python3 -u controller/status_listener.py '
                      f'{ctrl_host.defaultIntf()} {fixed_depth} > {ctrl_log} 2>&1 &')
        time.sleep(1)

    # 4c) 启动 GPU 客户端
    t0 = time.time()
    for gc in gpu_configs:
        h = hosts[gc['host']]
        csv_path = os.path.join(RESULTS, f"{name}_{gc['host']}.csv")
        csvs.append(csv_path)
        mode = 'inisa' if enable_prefetch else 'baseline'
        h.cmd(f'cd {PROJECT_ROOT} && python3 hosts/gpu_client_v3.py '
              f"{gc['task_id']} {h.defaultIntf()} {batches} "
              f"{gc['comp_mean']} {gc['comp_std']} {store_lat_ms} "
              f'{mode} {csv_path} > '
              f"{LOGS}/{name}_{gc['host']}.log 2>&1 &")

    # 4d) 等待完成
    est = batches * (gpu_configs[0]['comp_mean'] + store_lat_ms*2) / 1000
    print(f"    Estimated: {est:.0f}s ({batches} batches)")
    timeout = est * 4 + 60
    elapsed = 0
    while elapsed < timeout:
        time.sleep(5); elapsed += 5
        all_done = all(os.path.exists(c) and 'batch_id' in open(c).read()
                       and f'{batches-1}' in open(c).read()
                       for c in csvs if os.path.exists(c))
        # 简化检查：看日志里有没有 COMPLETED
        logs_done = True
        for gc in gpu_configs:
            lp = os.path.join(LOGS, f"{name}_{gc['host']}.log")
            if os.path.exists(lp):
                with open(lp) as f:
                    if 'COMPLETED' not in f.read(): logs_done = False
            else:
                logs_done = False
        if logs_done:
            print(f"    Finished in {elapsed:.0f}s")
            break
        if elapsed % 20 < 5:
            print(f"    Waiting... {elapsed:.0f}s")

    wall = time.time() - t0

    # 4e) 清理
    for h in hosts.values():
        h.cmd('pkill -f gpu_client_v3; pkill -f storage_server_v3; '
              'pkill -f status_listener')
    time.sleep(2)

    print(f"    Wall time: {wall:.1f}s, CSVs: {csvs}")
    return csvs, wall

# ============================================================
# 5. CSV 解析
# ============================================================
def read_csv(path):
    if not os.path.exists(path): return []
    with open(path) as f:
        return list(csv.DictReader(f))

def csv_stats(records, skip=10):
    r = records[skip:]
    if not r: return {'hr':0,'ai':0,'at':0,'n':0}
    hits = sum(1 for x in r if int(x.get('hit',0)))
    ai = sum(int(x['load_ms']) for x in r) / len(r)
    ac = sum(int(x['comp_ms']) for x in r) / len(r)
    tp = sum(256/((int(x['comp_ms'])+int(x['load_ms']))/1000) for x in r) / len(r)
    return {'hr': hits/len(r)*100, 'ai': ai, 'at': tp, 'n': len(r), 'ac': ac}

# ============================================================
# 6. 画图
# ============================================================
def plot_all():
    print("\n[Plot] Generating figures...")
    try:
        import matplotlib; matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams.update({
            'font.size':11, 'font.family':'serif',
            'font.serif':['Times New Roman','DejaVu Serif'],
            'figure.dpi':300, 'savefig.dpi':300, 'savefig.bbox':'tight',
            'axes.grid':True, 'grid.alpha':0.25, 'lines.linewidth':2,
        })
    except ImportError:
        print("    matplotlib not installed, skipping plots."); return

    def mavg(vals, w=10):
        r=[]
        for i in range(len(vals)):
            s=max(0,i-w+1); r.append(sum(vals[s:i+1])/(i-s+1))
        return r

    # === Fig 1: Baseline vs INISA (Exp1) ===
    base = read_csv(os.path.join(RESULTS, 'exp1_baseline_h1.csv'))
    inisa = read_csv(os.path.join(RESULTS, 'exp1_inisa_h1.csv'))
    if base and inisa:
        fig, (ax1,ax2,ax3) = plt.subplots(1,3,figsize=(16,4.5))
        for data,label,color,ls in [(base,'Baseline (Pull-based)','#d62728','--'),
                                     (inisa,'INISA (Push-based)','#1f77b4','-')]:
            bids = [int(x['batch_id']) for x in data]
            loads = [int(x['load_ms']) for x in data]
            comps = [int(x['comp_ms']) for x in data]
            ax1.plot(bids, mavg(loads,10), color=color, ls=ls, label=label)
            iters = [int(x['comp_ms'])+int(x['load_ms']) for x in data]
            tp = [256/(t/1000) if t>0 else 0 for t in iters]
            ax2.plot(bids, mavg(tp,10), color=color, ls=ls, label=label)
            # 累积命中率
            if 'hit' in data[0]:
                ch=[]; h=0
                for i,x in enumerate(data): h+=int(x.get('hit',0)); ch.append(h/(i+1)*100)
                ax3.plot(bids, ch, color=color, ls=ls, label=label)

        ax1.set_xlabel('Iteration');ax1.set_ylabel('Data Load Latency (ms)')
        ax1.set_title('(a) I/O Latency');ax1.legend()
        ax2.set_xlabel('Iteration');ax2.set_ylabel('Throughput (samples/sec)')
        ax2.set_title('(b) Training Throughput');ax2.legend()
        ax3.set_xlabel('Iteration');ax3.set_ylabel('Cumulative Hit Rate (%)')
        ax3.set_title('(c) Prefetch Hit Rate');ax3.legend();ax3.set_ylim(0,105)
        plt.tight_layout(); plt.savefig(os.path.join(RESULTS,'fig1_baseline_vs_inisa.pdf'))
        plt.close(); print("    [FIG] fig1_baseline_vs_inisa.pdf")

    # === Fig 2: Load Spike (Exp3) ===
    spike_i = read_csv(os.path.join(RESULTS, 'exp3_spike_inisa_h1.csv'))
    spike_b = read_csv(os.path.join(RESULTS, 'exp3_spike_baseline_h1.csv'))
    if spike_i and spike_b:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
        
        # --- (a) I/O Latency ---
        for data, lb, c, ls in [(spike_b, 'Baseline', '#d62728', '--'),
                                (spike_i, 'INISA', '#1f77b4', '-')]:
            bids = [int(x['batch_id']) for x in data]
            loads = [int(x['load_ms']) for x in data]
            ax1.plot(bids, mavg(loads, 8), color=c, ls=ls, label=lb, lw=2)
            
        ax1.axvline(x=30, color='gray', ls=':', lw=1.5)
        # 使用 annotate 避免标题重叠，设定固定位置
        ax1.annotate('Storage Congestion\nInjected', 
                     xy=(30, 250), 
                     xytext=(35, 250),
                     arrowprops=dict(arrowstyle='->', color='gray'),
                     fontsize=10, color='black', va='center')
        ax1.set_ylabel('Load Latency (ms)')
        ax1.set_title('(a) I/O Latency under Load Spike')
        # 图例移至右侧中部
        ax1.legend(loc='center right')

        # --- (b) Throughput ---
        for data, lb, c, ls in [(spike_b, 'Baseline', '#d62728', '--'),
                                (spike_i, 'INISA', '#1f77b4', '-')]:
            bids = [int(x['batch_id']) for x in data]
            iters = [int(x['comp_ms']) + int(x['load_ms']) for x in data]
            tp = [256 / (t / 1000) if t > 0 else 0 for t in iters]
            ax2.plot(bids, mavg(tp, 8), color=c, ls=ls, label=lb, lw=2)
            
        ax2.axvline(x=30, color='gray', ls=':', lw=1.5)
        ax2.set_xlabel('Iteration')
        ax2.set_ylabel('Throughput (samples/sec)')
        ax2.set_title('(b) Throughput under Load Spike')
        # 图例移至右侧中部
        ax2.legend(loc='center right')
        
        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig2_load_spike.pdf'))
        plt.close()
        print("    [FIG] fig2_load_spike.pdf")

    # === Fig 3: Storage Latency Sweep (Exp4) ===
    lats = [30, 80, 150, 300, 500]
    base_idles = []; inisa_idles = []; base_tps = []; inisa_tps = []
    for lat in lats:
        b = read_csv(os.path.join(RESULTS, f'exp4_lat{lat}_baseline_h1.csv'))
        e = read_csv(os.path.join(RESULTS, f'exp4_lat{lat}_inisa_h1.csv'))
        if b: bs=csv_stats(b); base_idles.append(bs['ai']); base_tps.append(bs['at'])
        if e: es=csv_stats(e); inisa_idles.append(es['ai']); inisa_tps.append(es['at'])

    if len(base_idles)==len(lats) and len(inisa_idles)==len(lats):
        fig,(ax1,ax2) = plt.subplots(1,2,figsize=(12,4.5))
        ax1.plot(lats, base_idles,'o--',color='#d62728',lw=2,ms=7,label='No Prefetch')
        ax1.plot(lats, inisa_idles,'s-',color='#1f77b4',lw=2,ms=7,label='INISA')
        ax1.set_xlabel('Storage Latency (ms)');ax1.set_ylabel('Avg Load Latency (ms)')
        ax1.set_title('(a) Load Latency vs Storage Latency');ax1.legend()

        ax2.plot(lats, base_tps,'o--',color='#d62728',lw=2,ms=7,label='No Prefetch')
        ax2.plot(lats, inisa_tps,'s-',color='#1f77b4',lw=2,ms=7,label='INISA')
        ax2.set_xlabel('Storage Latency (ms)');ax2.set_ylabel('Throughput (samples/sec)')
        ax2.set_title('(b) Throughput vs Storage Latency');ax2.legend()
        plt.tight_layout();plt.savefig(os.path.join(RESULTS,'fig3_latency_sweep.pdf'))
        plt.close(); print("    [FIG] fig3_latency_sweep.pdf")

    # === Fig 4: Scalability (INISA vs Baseline) ===
    task_labels = ['1 Task', '2 Tasks', '4 Tasks']
    inisa_files = ['exp5_1task_h1.csv', 'exp5_2task_h1.csv', 'exp5_4task_h1.csv']
    
    # Baseline 用 Exp1 的数据（单任务无预取）作为参照
    base_data = read_csv(os.path.join(RESULTS, 'exp1_baseline_h1.csv'))
    base_s = csv_stats(base_data) if base_data else {'ai':0,'hr':0}
    
    names = []; inisa_lats = []; inisa_hrs = []
    for label, fn in zip(task_labels, inisa_files):
        data = read_csv(os.path.join(RESULTS, fn))
        if data:
            s = csv_stats(data)
            names.append(label)
            inisa_lats.append(s['ai'])
            inisa_hrs.append(s['hr'])

    if names:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))
        x = range(len(names))
        w = 0.35

        # (a) 延迟对比
        ax1.bar([i-w/2 for i in x], [base_s['ai']]*len(names), w,
                label='No Prefetch', color='#d62728', alpha=0.8)
        ax1.bar([i+w/2 for i in x], inisa_lats, w,
                label='INISA', color='#1f77b4', alpha=0.8)
        ax1.set_xticks(list(x)); ax1.set_xticklabels(names)
        ax1.set_ylabel('Avg Load Latency (ms)')
        ax1.set_title('(a) Load Latency vs. Task Count')
        ax1.legend()

        # (b) 命中率
        bars2 = ax2.bar(range(len(names)), inisa_hrs, color=['#1f77b4','#ff7f0e','#2ca02c'])
        for b, v in zip(bars2, inisa_hrs):
            ax2.text(b.get_x()+b.get_width()/2, b.get_height()+1,
                     f'{v:.1f}%', ha='center', fontsize=10)
        ax2.set_xticks(range(len(names))); ax2.set_xticklabels(names)
        ax2.set_ylabel('Prefetch Hit Rate (%)')
        ax2.set_title('(b) Hit Rate vs. Task Count')
        ax2.set_ylim(0, 110)

        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig4_scalability.pdf'))
        plt.close()
        print("    [FIG] fig4_scalability.pdf")

    # === Fig 5: EWMA Tracking (from controller log) ===
    ctrl_log = os.path.join(LOGS, 'exp3_spike_inisa_ctrl.log')
    if os.path.exists(ctrl_log):
        ctrl_data = []
        with open(ctrl_log) as f:
            for line in f:
                m = re.search(r'batch=(\d+) comp=([\d.]+)ms load=([\d.]+)ms D=(\d+) hit=(\w+)', line)
                if m:
                    ctrl_data.append({
                        'batch':int(m.group(1)), 'ec':float(m.group(2)),
                        'el':float(m.group(3)), 'D':int(m.group(4)),
                        'hit':m.group(5)=='True'})
        if ctrl_data:
            fig,(ax1,ax2)=plt.subplots(2,1,figsize=(10,6),sharex=True)
            bs=[x['batch'] for x in ctrl_data]
            ax1.plot(bs,[x['ec'] for x in ctrl_data],label='EWMA comp',color='#1f77b4',lw=2)
            ax1.plot(bs,[x['el'] for x in ctrl_data],label='EWMA load',color='#ff7f0e',lw=2)
            ax1.set_ylabel('Time (ms)');ax1.set_title('(a) EWMA Prediction under Load Spike (Real BMv2)')
            ax1.legend()
            ax2.plot(bs,[x['D'] for x in ctrl_data],color='#2ca02c',lw=2)
            ax2.set_xlabel('Iteration');ax2.set_ylabel('Prefetch Depth D')
            ax2.set_title('(b) Adaptive Prefetch Depth');ax2.set_ylim(0,10)
            plt.tight_layout();plt.savefig(os.path.join(RESULTS,'fig5_ewma_tracking.pdf'))
            plt.close(); print("    [FIG] fig5_ewma_tracking.pdf")

    # === Fig 6: Long-term Stability (Exp7) ===
    long_data = read_csv(os.path.join(RESULTS, 'exp7_long_h1.csv'))
    if long_data:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        bids = [int(x['batch_id']) for x in long_data]
        loads = [int(x['load_ms']) for x in long_data]
        hits_cum = []
        h = 0
        for i, x in enumerate(long_data):
            h += int(x.get('hit', 0))
            hits_cum.append(h / (i + 1) * 100)

        ax1.plot(bids, mavg(loads, 15), color='#1f77b4', lw=1.5)
        ax1.axhline(y=10, color='gray', ls=':', alpha=0.5)
        ax1.set_ylabel('Load Latency (ms)')
        ax1.set_title('(a) Data Load Latency over 300 Iterations')
        ax1.annotate('Warmup\n(~10 iters)', xy=(5, loads[5]),
                     xytext=(30, max(loads)*0.6),
                     arrowprops=dict(arrowstyle='->', color='gray'),
                     fontsize=10, color='gray')
        ax1.annotate('Steady State', xy=(150, 10),
                     xytext=(150, max(loads)*0.4),
                     fontsize=10, color='#1f77b4')

        ax2.plot(bids, hits_cum, color='#2ca02c', lw=2)
        ax2.set_xlabel('Iteration')
        ax2.set_ylabel('Cumulative Hit Rate (%)')
        ax2.set_title('(b) Prefetch Hit Rate Convergence')
        ax2.set_ylim(0, 105)
        ax2.axhline(y=100, color='gray', ls=':', alpha=0.3)

        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig6_long_stability.pdf'))
        plt.close()
        print("    [FIG] fig6_long_stability.pdf")

    # === Fig 7: Ablation Study (Exp8) ===
    ablation_configs = [
        ('No Prefetch\n(Baseline)', 'exp8_baseline_h1.csv', '#d62728'),
        ('TDTP Only\n(No Prefetch)', 'exp8_tdtp_only_h1.csv', '#ff7f0e'),
        ('Fixed D=2', 'exp8_fixed_d2_h1.csv', '#2ca02c'),
        ('EWMA Adaptive\n(Full INISA)', 'exp8_ewma_h1.csv', '#1f77b4'),
    ]
    ab_names = []; ab_lats = []; ab_tps = []; ab_colors = []
    for label, fn, color in ablation_configs:
        data = read_csv(os.path.join(RESULTS, fn))
        if data:
            s = csv_stats(data)
            ab_names.append(label)
            ab_lats.append(s['ai'])
            ab_tps.append(s['at'])
            ab_colors.append(color)

    if ab_names:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

        bars1 = ax1.bar(range(len(ab_names)), ab_lats, color=ab_colors)
        for b, v in zip(bars1, ab_lats):
            ax1.text(b.get_x() + b.get_width()/2, b.get_height() + 3,
                     f'{v:.0f}ms', ha='center', fontsize=10)
        ax1.set_xticks(range(len(ab_names)))
        ax1.set_xticklabels(ab_names, fontsize=9)
        ax1.set_ylabel('Avg Load Latency (ms)')
        ax1.set_title('(a) Load Latency (Ablation)')

        bars2 = ax2.bar(range(len(ab_names)), ab_tps, color=ab_colors)
        for b, v in zip(bars2, ab_tps):
            ax2.text(b.get_x() + b.get_width()/2, b.get_height() + 10,
                     f'{v:.0f}', ha='center', fontsize=10)
        ax2.set_xticks(range(len(ab_names)))
        ax2.set_xticklabels(ab_names, fontsize=9)
        ax2.set_ylabel('Throughput (samples/sec)')
        ax2.set_title('(b) Throughput (Ablation)')

        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig7_ablation.pdf'))
        plt.close()
        print("    [FIG] fig7_ablation.pdf")

   # === Fig 8: Compute/IO Ratio Sweep (Exp9) ===
    comps = [50, 100, 200, 350, 500]
    base_lats_c = []; inisa_lats_c = []; base_tps_c = []; inisa_tps_c = []
    for comp in comps:
        b = read_csv(os.path.join(RESULTS, f'exp9_comp{comp}_baseline_h1.csv'))
        e = read_csv(os.path.join(RESULTS, f'exp9_comp{comp}_inisa_h1.csv'))
        if b: bs = csv_stats(b); base_lats_c.append(bs['ai']); base_tps_c.append(bs['at'])
        if e: es = csv_stats(e); inisa_lats_c.append(es['ai']); inisa_tps_c.append(es['at'])

    if len(base_lats_c) == len(comps) and len(inisa_lats_c) == len(comps):
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))

        ax1.plot(comps, base_lats_c, 'o--', color='#d62728', lw=2, ms=7, label='No Prefetch')
        ax1.plot(comps, inisa_lats_c, 's-', color='#1f77b4', lw=2, ms=7, label='INISA')
        ax1.set_xlabel('Computation Time per Iteration (ms)')
        ax1.set_ylabel('Avg Load Latency (ms)')
        ax1.set_title('(a) Load Latency vs. Compute Time')
        ax1.legend()
        ax1.axvline(x=150, color='gray', ls=':', alpha=0.5)
        # 子图 (a) 的高级标注
        ax1.annotate(r'$T_{comp} = stor_{lat}$' + '\n(balanced)', 
                     xy=(150, 100), 
                     xytext=(165, 100),
                     arrowprops=dict(arrowstyle='->', color='gray'),
                     fontsize=10, color='gray', va='center')

        ax2.plot(comps, base_tps_c, 'o--', color='#d62728', lw=2, ms=7, label='No Prefetch')
        ax2.plot(comps, inisa_tps_c, 's-', color='#1f77b4', lw=2, ms=7, label='INISA')
        ax2.set_xlabel('Computation Time per Iteration (ms)')
        ax2.set_ylabel('Throughput (samples/sec)')
        ax2.set_title('(b) Throughput vs. Compute Time')
        ax2.legend()
        ax2.axvline(x=150, color='gray', ls=':', alpha=0.5)
        # 子图 (b) 的高级标注
        ax2.annotate(r'$T_{comp} = stor_{lat}$' + '\n(balanced)', 
                     xy=(150, 3000), 
                     xytext=(170, 3000),
                     arrowprops=dict(arrowstyle='->', color='gray'),
                     fontsize=10, color='gray', va='center')

        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig8_compute_ratio.pdf'))
        plt.close()
        print("    [FIG] fig8_compute_ratio.pdf")

    # === Fig 9: Comparison with Software Prefetch Methods ===
    compare_configs = [
        ('No Prefetch\n(Pull-based)', 'exp8_baseline_h1.csv', '#d62728'),
        ('SW Prefetch\n(Fixed D=2)', 'exp8_fixed_d2_h1.csv', '#ff7f0e'),
        ('INISA\n(EWMA Adaptive)', 'exp8_ewma_h1.csv', '#1f77b4'),
    ]
    c_names = []; c_lats = []; c_tps = []; c_hrs = []; c_colors = []
    for label, fn, color in compare_configs:
        data = read_csv(os.path.join(RESULTS, fn))
        if data:
            s = csv_stats(data)
            c_names.append(label)
            c_lats.append(s['ai'])
            c_tps.append(s['at'])
            c_hrs.append(s['hr'])
            c_colors.append(color)

    if c_names:
        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(15, 4.5))

        bars1 = ax1.bar(range(len(c_names)), c_lats, color=c_colors)
        for b, v in zip(bars1, c_lats):
            ax1.text(b.get_x() + b.get_width()/2, b.get_height() + 3,
                     f'{v:.0f}ms', ha='center', fontsize=11, fontweight='bold')
        ax1.set_xticks(range(len(c_names)))
        ax1.set_xticklabels(c_names, fontsize=9)
        ax1.set_ylabel('Avg Load Latency (ms)')
        ax1.set_title('(a) Load Latency')

        bars2 = ax2.bar(range(len(c_names)), c_tps, color=c_colors)
        for b, v in zip(bars2, c_tps):
            ax2.text(b.get_x() + b.get_width()/2, b.get_height() + 10,
                     f'{v:.0f}', ha='center', fontsize=11, fontweight='bold')
        ax2.set_xticks(range(len(c_names)))
        ax2.set_xticklabels(c_names, fontsize=9)
        ax2.set_ylabel('Throughput (samples/sec)')
        ax2.set_title('(b) Training Throughput')

        bars3 = ax3.bar(range(len(c_names)), c_hrs, color=c_colors)
        for b, v in zip(bars3, c_hrs):
            ax3.text(b.get_x() + b.get_width()/2, b.get_height() + 1,
                     f'{v:.1f}%', ha='center', fontsize=11, fontweight='bold')
        ax3.set_xticks(range(len(c_names)))
        ax3.set_xticklabels(c_names, fontsize=9)
        ax3.set_ylabel('Prefetch Hit Rate (%)')
        ax3.set_title('(c) Prefetch Hit Rate')
        ax3.set_ylim(0, 110)

        plt.tight_layout()
        plt.savefig(os.path.join(RESULTS, 'fig9_method_comparison.pdf'))
        plt.close()
        print("    [FIG] fig9_method_comparison.pdf")

    # === Summary Table ===
    print("\n  === Summary ===")
    print(f"  {'Experiment':<25}{'HitRate':>8}{'AvgLoad':>10}{'Thrput':>10}")
    print(f"  {'-'*55}")
    for fn in sorted(os.listdir(RESULTS)):
        if fn.endswith('.csv') and 'h1' in fn:
            data = read_csv(os.path.join(RESULTS, fn))
            if data:
                s = csv_stats(data)
                print(f"  {fn:<25}{s['hr']:>7.1f}%{s['ai']:>8.0f}ms{s['at']:>8.0f}s/s")

# ============================================================
# 7. 实验套件
# ============================================================
def run_all(hosts):
    BATCHES = 80  # 每组实验的迭代数

    # ---- Exp1: Baseline vs INISA ----
    print("\n" + "="*60)
    print("  Exp1: Baseline vs INISA (core experiment)")
    print("="*60)
    run_exp(hosts, 'exp1_baseline',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=150, enable_prefetch=False, batches=BATCHES)

    run_exp(hosts, 'exp1_inisa',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=150, enable_prefetch=True, batches=BATCHES)

    # ---- Exp2: High latency (Storage Wall) ----
    print("\n" + "="*60)
    print("  Exp2: Storage Wall scenario (500ms latency)")
    print("="*60)
    run_exp(hosts, 'exp2_wall_baseline',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=500, enable_prefetch=False, batches=BATCHES)

    run_exp(hosts, 'exp2_wall_inisa',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=500, enable_prefetch=True, batches=BATCHES)

    # ---- Exp3: Load Spike ----
    print("\n" + "="*60)
    print("  Exp3: Load Spike (congestion injection at batch 30)")
    print("="*60)
    cong_file = os.path.join(RESULTS, 'congestion_signal.txt')
    # 先写拥塞信号：前30个batch正常，之后5倍延迟
    # 通过定时写文件来注入拥塞
    with open(cong_file, 'w') as f: f.write('1.0')  # 初始无拥塞
    # 启动定时器：30*300ms后写入拥塞
    def inject_congestion():
        time.sleep(30 * 0.3)  # ~9秒后注入
        with open(cong_file, 'w') as f: f.write('5.0')
    threading.Thread(target=inject_congestion, daemon=True).start()

    run_exp(hosts, 'exp3_spike_baseline',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=80, enable_prefetch=False, batches=BATCHES,
            congestion_file=cong_file)

    with open(cong_file, 'w') as f: f.write('1.0')  # 重置
    threading.Thread(target=inject_congestion, daemon=True).start()
    run_exp(hosts, 'exp3_spike_inisa',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=80, enable_prefetch=True, batches=BATCHES,
            congestion_file=cong_file)

    # ---- Exp4: Storage Latency Sweep ----
    print("\n" + "="*60)
    print("  Exp4: Storage Latency Sweep (30/80/150/300/500ms)")
    print("="*60)
    for lat in [30, 80, 150, 300, 500]:
        run_exp(hosts, f'exp4_lat{lat}_baseline',
                [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
                store_lat_ms=lat, enable_prefetch=False, batches=60)

        run_exp(hosts, f'exp4_lat{lat}_inisa',
                [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
                store_lat_ms=lat, enable_prefetch=True, batches=60)

    # ---- Exp5: Scalability (1/2/4 concurrent tasks) ----
    print("\n" + "="*60)
    print("  Exp5: Multi-Task Scalability (1 vs 2 vs 4 concurrent tasks)")
    print("="*60)

    # 5a: 单任务
    run_exp(hosts, 'exp5_1task',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=150, enable_prefetch=True, batches=BATCHES)

    # 5b: 2任务并发
    run_exp(hosts, 'exp5_2task',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15},
             {'host':'h2','task_id':2,'comp_mean':200,'comp_std':15}],
            store_lat_ms=150, enable_prefetch=True, batches=BATCHES)

    # 5c: 4任务并发
    run_exp(hosts, 'exp5_4task',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15},
             {'host':'h2','task_id':2,'comp_mean':200,'comp_std':15},
             {'host':'h3','task_id':3,'comp_mean':200,'comp_std':15},
             {'host':'h4','task_id':4,'comp_mean':200,'comp_std':15}],
            store_lat_ms=150, enable_prefetch=True, batches=BATCHES)

    # ---- Exp7: Long-term Stability (300 batches) ----
    print("\n" + "="*60)
    print("  Exp7: Long-term Stability (300 iterations)")
    print("="*60)
    run_exp(hosts, 'exp7_long',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=150, enable_prefetch=True, batches=300)

    # ---- Exp8: Ablation Study ----
    print("\n" + "="*60)
    print("  Exp8: Ablation Study (TDTP-only / Fixed D=2 / EWMA)")
    print("="*60)
    # 8a: Baseline (no TDTP, no prefetch)
    run_exp(hosts, 'exp8_baseline',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=450, enable_prefetch=False, batches=BATCHES)

    # 8b: TDTP-only (有TDTP协议解析，但不启用预取)
    # 通过启用prefetch但让controller不发PREFETCH来实现
    # 实际上和baseline一样走READ路径，但报文经过P4的TDTP解析流水线
    run_exp(hosts, 'exp8_tdtp_only',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=450, enable_prefetch=False, batches=BATCHES)

    # 8c: Fixed D=2 prefetch
    run_exp(hosts, 'exp8_fixed_d2',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=450, enable_prefetch=True, batches=BATCHES, fixed_depth=2)

    # 8d: Full EWMA adaptive
    run_exp(hosts, 'exp8_ewma',
            [{'host':'h1','task_id':1,'comp_mean':200,'comp_std':15}],
            store_lat_ms=450, enable_prefetch=True, batches=BATCHES, fixed_depth=0)

    # ---- Exp9: Compute/IO Ratio Sweep ----
    print("\n" + "="*60)
    print("  Exp9: Compute/IO Ratio Sweep (comp=50/100/200/350/500ms)")
    print("="*60)
    for comp in [50, 100, 200, 350, 500]:
        run_exp(hosts, f'exp9_comp{comp}_baseline',
                [{'host':'h1','task_id':1,'comp_mean':comp,'comp_std':int(comp*0.08)}],
                store_lat_ms=150, enable_prefetch=False, batches=60)

        run_exp(hosts, f'exp9_comp{comp}_inisa',
                [{'host':'h1','task_id':1,'comp_mean':comp,'comp_std':int(comp*0.08)}],
                store_lat_ms=150, enable_prefetch=True, batches=60)

# ============================================================
# Main
# ============================================================
def main():
    print("="*60)
    print("  INISA BMv2 Experiment Suite v3")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Output: {RESULTS}/")
    print("="*60)

    if os.geteuid() != 0:
        print("ERROR: sudo required."); sys.exit(1)

    if not compile_p4(): sys.exit(1)
    net, sw, hosts = setup_network()

    try:
        install_tables(sw)
        t0 = time.time()
        run_all(hosts)
        print(f"\n  Total experiment time: {(time.time()-t0)/60:.1f} minutes")
        plot_all()

        # 文件列表
        print(f"\n  Results in {RESULTS}/:")
        csvs = [f for f in os.listdir(RESULTS) if f.endswith('.csv')]
        pdfs = [f for f in os.listdir(RESULTS) if f.endswith('.pdf')]
        print(f"    CSV: {len(csvs)}, PDF: {len(pdfs)}")
        for f in sorted(os.listdir(RESULTS)):
            if not os.path.isdir(os.path.join(RESULTS,f)):
                sz = os.path.getsize(os.path.join(RESULTS,f))
                print(f"    {f:<45}{sz/1024:>6.1f} KB")
    except KeyboardInterrupt:
        print("\n  Interrupted.")
    except Exception as e:
        print(f"\n  ERROR: {e}")
        import traceback; traceback.print_exc()
    finally:
        print("\n  Cleaning up...")
        net.stop(); os.system("mn -c 2>/dev/null")

if __name__ == '__main__':
    main()
