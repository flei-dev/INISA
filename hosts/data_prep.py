#!/usr/bin/env python3
"""
data_prep.py - 准备 CIFAR-10 真实训练数据
==========================================
下载 CIFAR-10，将图片提取为单独文件，分配到 4 个存储节点目录。

运行: python3 hosts/data_prep.py
产出: data/store{1-4}/ 各含 ~12500 张 .bin 文件 (每张 3073 字节 = 3072像素 + 1标签)
"""
import os, sys, struct, pickle, hashlib
import urllib.request, tarfile

DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
CIFAR_URL = "https://www.cs.toronto.edu/~kriz/cifar-10-python.tar.gz"
CIFAR_DIR = os.path.join(DATA_ROOT, 'cifar-10-batches-py')

def download_cifar10():
    """下载 CIFAR-10 数据集"""
    os.makedirs(DATA_ROOT, exist_ok=True)
    tar_path = os.path.join(DATA_ROOT, 'cifar-10-python.tar.gz')
    
    if os.path.exists(CIFAR_DIR):
        print("  CIFAR-10 already exists, skipping download.")
        return
    
    if not os.path.exists(tar_path):
        print(f"  Downloading CIFAR-10 from {CIFAR_URL}...")
        urllib.request.urlretrieve(CIFAR_URL, tar_path)
        print(f"  Downloaded: {os.path.getsize(tar_path) / 1024 / 1024:.1f} MB")
    
    print("  Extracting...")
    with tarfile.open(tar_path) as tar:
        tar.extractall(DATA_ROOT)
    print("  Extraction complete.")

def load_cifar_batch(filepath):
    """加载一个 CIFAR-10 batch 文件"""
    with open(filepath, 'rb') as f:
        d = pickle.load(f, encoding='bytes')
    return d[b'data'], d[b'labels']

def prepare_data():
    """将 CIFAR-10 拆分为单独的样本文件，分配到 4 个存储节点"""
    print("=" * 60)
    print("  CIFAR-10 Data Preparation for INISA Storage Nodes")
    print("=" * 60)
    
    download_cifar10()
    
    # 创建 4 个存储节点的数据目录
    for i in range(1, 5):
        os.makedirs(os.path.join(DATA_ROOT, f'store{i}'), exist_ok=True)
    
    # 加载所有训练数据
    all_images = []
    all_labels = []
    for batch_id in range(1, 6):
        path = os.path.join(CIFAR_DIR, f'data_batch_{batch_id}')
        images, labels = load_cifar_batch(path)
        all_images.append(images)
        all_labels.extend(labels)
    
    all_images = __import__('numpy').concatenate(all_images, axis=0) if 'numpy' in sys.modules else None
    
    # 如果没有 numpy，用纯 Python 处理
    if all_images is None:
        all_images_raw = []
        for batch_id in range(1, 6):
            path = os.path.join(CIFAR_DIR, f'data_batch_{batch_id}')
            with open(path, 'rb') as f:
                d = pickle.load(f, encoding='bytes')
            all_images_raw.extend(d[b'data'].tolist() if hasattr(d[b'data'], 'tolist') else list(d[b'data']))
    else:
        all_images_raw = all_images.tolist()
    
    total_samples = len(all_labels)
    print(f"  Total samples: {total_samples}")
    
    # 每个样本保存为: [label(1B)] + [pixel_data(3072B)] = 3073 字节
    # 分配规则: sample_id % 4 → store{1-4}
    counts = [0, 0, 0, 0]
    checksums = {}
    
    for sample_id in range(total_samples):
        store_id = (sample_id % 4) + 1
        label = all_labels[sample_id]
        pixels = all_images_raw[sample_id]
        
        # 序列化: 1字节标签 + 3072字节像素
        if isinstance(pixels, list):
            data = struct.pack('B', label) + bytes(pixels)
        else:
            data = struct.pack('B', label) + bytes(pixels)
        
        filepath = os.path.join(DATA_ROOT, f'store{store_id}', f'sample_{sample_id}.bin')
        with open(filepath, 'wb') as f:
            f.write(data)
        
        counts[store_id - 1] += 1
        checksums[sample_id] = hashlib.md5(data).hexdigest()[:8]
    
    # 保存校验和文件（GPU 端用于验证数据完整性）
    cksum_path = os.path.join(DATA_ROOT, 'checksums.txt')
    with open(cksum_path, 'w') as f:
        for sid, ck in sorted(checksums.items()):
            f.write(f"{sid} {ck}\n")
    
    print(f"  Samples per store: {counts}")
    print(f"  Sample size: 3073 bytes (1B label + 3072B pixels)")
    print(f"  Checksums saved to: {cksum_path}")
    print(f"  Data directory: {DATA_ROOT}/")
    print("  Done!")

if __name__ == '__main__':
    try:
        import numpy as np
    except ImportError:
        print("  WARNING: numpy not installed, using pure Python (slower)")
    prepare_data()
