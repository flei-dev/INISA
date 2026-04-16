# INISA Prototype: Training-Aware Push-Based Data Supply on P4 Programmable Switches

## Overview

This repository contains the BMv2 software prototype for **INISA** (In-Network Intelligent Storage Architecture), a P4-based training-aware data prefetch system for AI training storage acceleration. The prototype includes two experimental layers:

1. **Controlled BMv2 experiments** (9 groups): validate INISA's core mechanisms under synthetic workloads with Mininet topology.
2. **Real PyTorch training experiments** (2 groups): validate end-to-end performance and convergence equivalence using a real CNN trained on CIFAR-10.

All experiments run on real BMv2 software switches — TDTP packets are parsed, routed, and prioritized by the actual P4 data plane, not emulated.

> **Note:** The Tofino hardware deployment (reported in Section 6.4 of the paper) is not included in this repository due to Intel P4 Studio SDE licensing restrictions. The TNA port of the P4 code follows the v1model→TNA API mapping documented in the paper (Table 5-3).

## Architecture
```
GPU Nodes (h1-h4)  ←→  BMv2 P4 Switch (s1)  ←→  Storage Nodes (st1-st4)
                              ↕
                    Controller (EWMA prefetch engine)
```

- **P4 Data Plane**: TDTP protocol parsing, object address translation, ACK reverse routing, priority classification, per-task metering
- **Controller**: EWMA-based adaptive prefetch depth, independent PREFETCH RTT measurement
- **GPU Client**: Training iteration simulation (synthetic) or real PyTorch training; background ACK sniffer for prefetch hit detection
- **Storage Server**: Configurable latency simulation, congestion signal support, real CIFAR-10 data sharding

## Prerequisites

- Ubuntu 20.04/22.04 (P4 Tutorials VM recommended)
- BMv2 (`simple_switch`), p4c compiler
- Mininet 2.3, Scapy 2.5, matplotlib 3.7
- For real training experiments: PyTorch 2.0.1 (CPU version), NumPy

## Quick Start

### Controlled BMv2 experiments (9 groups, ~20 min)
```bash
sudo python3 tests/mininet_experiment_driver_v3.py
```
Output: `results_bmv2_v3/` (20 CSV files + 9 PDF figures)

### Real PyTorch training experiments (2 groups, ~40 min)
```bash
# Step 1: download CIFAR-10 (once)
python3 hosts/data_prep.py

# Step 2: run real training + transport + ablation
sudo python3 tests/real_workload_experiment.py
```
Output: `results_real/` (4 CSV files + 3 PDF figures)

## Project Structure
```
├── p4src/                          # P4 data plane
│   ├── headers.p4                  # TDTP header (26 bytes)
│   ├── parser.p4                   # 4-state TDTP parser
│   ├── ingress.p4                  # Address translation + ACK reverse routing
│   ├── egress.p4                   # Egress pipeline + checksum
│   └── inisa.p4                    # Top-level assembly
├── controller/
│   ├── prefetch_engine.py          # EWMA prediction + adaptive depth D
│   └── status_listener.py          # STATUS listener + RTT measurement
├── hosts/
│   ├── gpu_client_v3.py            # GPU training simulation (synthetic)
│   ├── gpu_trainer_real.py         # Real PyTorch CNN trainer
│   ├── storage_server_v3.py        # Storage node (synthetic latency)
│   ├── storage_server_real.py      # Storage node (real CIFAR-10 data)
│   ├── data_prep.py                # Download and shard CIFAR-10
│   └── tdtp_packet.py              # TDTP Scapy protocol definition
├── tests/
│   ├── mininet_experiment_driver_v3.py  # 9 controlled experiments
│   └── real_workload_experiment.py      # 2 real PyTorch experiments
├── data/                           # CIFAR-10 data (gitignored, regenerated)
├── results_bmv2_v3/                # Controlled experiment outputs
└── results_real/                   # Real training experiment outputs
```

## Experiments

### Controlled BMv2 Experiments

| Fig | Experiment | Validates |
|-----|-----------|-----------|
| fig1 | Baseline vs INISA | Core latency/throughput improvement |
| fig2 | Load Spike | Robustness under storage congestion |
| fig3 | Storage Latency Sweep | Advantage grows with I/O pressure |
| fig4 | Multi-Task Scalability | No degradation with 1/2/4 concurrent tasks |
| fig5 | EWMA Tracking | Adaptive depth D under load spike |
| fig6 | Long-term Stability | 300-iteration sustained performance |
| fig7 | Ablation Study | Per-component contribution |
| fig8 | Compute/IO Ratio | Effectiveness across workload types |
| fig9 | Method Comparison | INISA vs fixed-depth software prefetch |

### Real PyTorch Training Experiments

| Fig | Experiment | Validates |
|-----|-----------|-----------|
| fig_real_transport | TDTP Transport Layer | Real CIFAR-10 data transport via TDTP |
| fig_real_training | End-to-End Training | Loss convergence equivalence (baseline vs INISA) |
| fig_real_ablation | Ablation on Real Workload | Component contribution under real training |

## TDTP Protocol

| Field | Size | Description |
|-------|------|-------------|
| Version | 1B | Protocol version |
| Opcode | 1B | READ/WRITE/STATUS/ACK/PREFETCH |
| TaskID | 2B | Training task identifier |
| BatchID | 4B | Mini-batch sequence number |
| ObjectID | 4B | Storage object identifier |
| SeqNum | 4B | TDTP sequence number |
| Offset | 4B | Object offset (also encodes comp_time in STATUS) |
| Length | 4B | Data length (also encodes load_time in STATUS) |
| Flags | 2B | PUSH/URGENT/LAST/PREFETCH |

## Reproducing Paper Results

The CSV files in `results_bmv2_v3/` and `results_real/` are the exact data used to generate the figures in the paper. To regenerate the figures from raw data without re-running experiments:
```bash
# Inside tests/mininet_experiment_driver_v3.py, call plot_all() directly
python3 -c "from tests.mininet_experiment_driver_v3 import plot_all; plot_all()"
```

## Citation

Citation information will be updated upon paper acceptance.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.