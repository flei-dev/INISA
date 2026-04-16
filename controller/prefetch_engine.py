#!/usr/bin/env python3
# prefetch_engine.py - 预取调度引擎（模拟 FPGA 智能层）
import math
import time
import threading
from collections import defaultdict

class TaskState:
    """ 训练任务状态（对应论文 1 中的 TaskState 结构体） """
    def __init__(self, task_id, total_samples, batch_size,
                 shuffle_seed=42):
        self.task_id = task_id
        self.batch_size = batch_size
        self.total_batches = total_samples // batch_size
        self.current_batch = 0
        self.current_epoch = 0

        # EWMA 估计
        self.ewma_comp = 200.0
        self.ewma_load = 50.0
        self.alpha = 0.3

        # 预取状态
        self.prefetch_depth = 2
        self.D_MIN = 2
        self.D_MAX = 8
        self.prefetched_batches = set()

        # Shuffle 索引
        import random
        rng = random.Random(shuffle_seed)
        self.shuffle_index = list(range(total_samples))
        rng.shuffle(self.shuffle_index)

    def get_batch_objects(self, batch_id):
        start = batch_id * self.batch_size
        end = min(start + self.batch_size, len(self.shuffle_index))
        return self.shuffle_index[start:end]

class PrefetchEngine:
    """ 预取调度引擎（对应论文 1 的算法 2） """

    def __init__(self, send_prefetch_fn, fixed_depth=0):
        self.tasks = {}
        self.send_prefetch = send_prefetch_fn
        self.fixed_depth = fixed_depth
        self.stats = defaultdict(lambda: {
            'prefetch_hits': 0,
            'prefetch_misses': 0,
            'total_requests': 0
        })

    def register_task(self, task_id, total_samples,
                      batch_size, shuffle_seed=42):
        self.tasks[task_id] = TaskState(
            task_id, total_samples, batch_size, shuffle_seed)
        print(f'[PrefetchEngine] Registered task {task_id}: '
              f'{total_samples} samples, bs={batch_size}, '
              f'{total_samples//batch_size} batches/epoch')

    def on_status_report(self, task_id, batch_id,
                         comp_time_ms, load_time_ms):
        if task_id not in self.tasks:
            return

        ts = self.tasks[task_id]

        # 1. 更新 EWMA 估计
        ts.ewma_comp = (ts.alpha * comp_time_ms +
                        (1 - ts.alpha) * ts.ewma_comp)
        ts.ewma_load = (ts.alpha * load_time_ms +
                        (1 - ts.alpha) * ts.ewma_load)

        # 2. 重新计算预取深度
        if self.fixed_depth > 0:
            ts.prefetch_depth = self.fixed_depth
        elif ts.ewma_comp > 0:
            D = math.ceil(ts.ewma_load / ts.ewma_comp) + 1
            ts.prefetch_depth = max(ts.D_MIN,
                                    min(ts.D_MAX, D))

        # 3. 更新进度
        ts.current_batch = batch_id

        # 4. 发射预取指令 —— 每个 batch 只发 1 个 PREFETCH 包
        for i in range(batch_id + 1,
                       batch_id + 1 + ts.prefetch_depth):
            if (i < ts.total_batches and
                i not in ts.prefetched_batches):
                # 只取该 batch 的第一个 object，发 1 个包代表整个 batch
                objects = ts.get_batch_objects(i)
                if objects:
                    self.send_prefetch(task_id, i, objects[0])
                ts.prefetched_batches.add(i)

        # 5. 淘汰已消费的预取数据
        ts.prefetched_batches.discard(batch_id - 1)

        # 日志
        hit = batch_id in ts.prefetched_batches
        self.stats[task_id]['total_requests'] += 1
        if hit:
            self.stats[task_id]['prefetch_hits'] += 1
        else:
            self.stats[task_id]['prefetch_misses'] += 1

        print(f'[PrefetchEngine] Task {task_id} '
              f'batch={batch_id} '
              f'comp={ts.ewma_comp:.1f}ms '
              f'load={ts.ewma_load:.1f}ms '
              f'D={ts.prefetch_depth} '
              f'hit={hit}')

    def get_hit_rate(self, task_id):
        s = self.stats[task_id]
        total = s['total_requests']
        if total == 0: return 0.0
        return s['prefetch_hits'] / total